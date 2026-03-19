import logging
import re

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_round

_logger = logging.getLogger(__name__)


class ConaiKgReportWizard(models.TransientModel):
    _name = "conai.kg.report.wizard"
    _description = "Wizard Report CONAI (da importi fatturati) per Fascia/Cliente"

    date_from = fields.Date(required=True, default=fields.Date.context_today)
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)

    def _find_base_product_from_conai_line(self, conai_aml, conai_product):
        """
        Risale al prodotto 'origine' a partire dalla riga fattura CONAI.

        Strategia 1 (robusta): usa sale_line_ids -> order_id -> trova la riga prodotto precedente per sequence.
        Fallback: prova a parsare il nome "Contributo ambientale CONAI per XXX" e fare name_search sul prodotto.
        """
        # --- Strategia 1: sale_line_ids (fattura da ordine) ---
        sale_lines = getattr(conai_aml, "sale_line_ids", False)
        if sale_lines:
            # prendo la prima sale line "conai"
            sl = sale_lines[0]
            order = getattr(sl, "order_id", False)
            if order:
                conai_seq = sl.sequence or 0
                # prendo la riga prodotto con sequence più alta ma < conai_seq (skip note/section + righe CONAI)
                candidates = order.order_line.filtered(
                    lambda l: (not l.display_type)
                    and l.product_id
                    and (l.product_id != conai_product)
                    and ((l.sequence or 0) < conai_seq)
                ).sorted(key=lambda l: (l.sequence or 0, l.id), reverse=True)

                if candidates:
                    return candidates[0].product_id

        # --- Fallback: parse del nome della riga ---
        name = (conai_aml.name or "").strip()
        if name:
            # prendo prima riga testo
            first_line = name.splitlines()[0].strip()
            # rimuovo prefisso noto
            prefix = "Contributo ambientale CONAI per "
            if first_line.startswith(prefix):
                prod_txt = first_line[len(prefix):].strip()
            else:
                # prova regex "per XXX"
                m = re.search(r"\bper\s+(.*)$", first_line)
                prod_txt = m.group(1).strip() if m else ""

            if prod_txt:
                # name_search su product.product
                res = self.env["product.product"].name_search(prod_txt, operator="ilike", limit=5)
                if res:
                    # se troviamo un match, prendo il primo
                    return self.env["product.product"].browse(res[0][0])

        return False

    def action_generate(self):
        self.ensure_one()

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("Intervallo date non valido: 'Dal' è successivo a 'Al'.")

        # ====== CONFIG ======
        ESENZIONE_PCT_FIELD = "x_studio_esenzione_conai_percentuale"  # su res.partner
        CONAI_M2O_FIELD = "x_studio_fascia_conai"                     # su product.product -> x_conai
        TARIFFA_TON_FIELD = "x_studio_tariffa_ton"                    # €/ton su x_conai
        CONAI_DEFAULT_CODE = "CONAI"                                  # default_code articolo CONAI
        # ====================

        # pulizia righe report precedenti (wizard)
        self.env["conai.kg.report.line"].search([("wizard_id", "=", self.id)]).unlink()

        conai_product = self.env["product.product"].search([("default_code", "=", CONAI_DEFAULT_CODE)], limit=1)
        if not conai_product:
            raise UserError(f"Prodotto CONAI non trovato (default_code='{CONAI_DEFAULT_CODE}').")

        _logger.info(
            "CONAI_REPORT_INV: start company=%s date_from=%s date_to=%s conai_product_id=%s",
            self.company_id.id, self.date_from, self.date_to, conai_product.id
        )

        # fatture/NC postate nel periodo: invoice_date OR date
        Move = self.env["account.move"]
        moves_domain = [
            ("state", "=", "posted"),
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("company_id", "=", self.company_id.id),
            "|",
            "&", ("invoice_date", ">=", self.date_from), ("invoice_date", "<=", self.date_to),
            "&", ("date", ">=", self.date_from), ("date", "<=", self.date_to),
        ]
        moves = Move.search(moves_domain)
        _logger.info("CONAI_REPORT_INV: moves found=%s", len(moves))

        if not moves:
            raise UserError(
                "Nessuna fattura/NC POSTATA trovata nel periodo.\n"
                "Nota: il report filtra per Invoice Date oppure Accounting Date (date)."
            )

        # prendo SOLO le righe fattura del prodotto CONAI (importi reali fatturati)
        Line = self.env["account.move.line"]
        conai_line_domain = [
            ("move_id", "in", moves.ids),
            ("product_id", "=", conai_product.id),
        ]
        if "exclude_from_invoice_tab" in Line._fields:
            conai_line_domain.append(("exclude_from_invoice_tab", "=", False))

        conai_amls = Line.search(conai_line_domain)
        _logger.info("CONAI_REPORT_INV: conai invoice lines found=%s", len(conai_amls))

        if not conai_amls:
            raise UserError(
                "Nel periodo non risultano righe fattura con prodotto CONAI.\n"
                "Se il CONAI è presente solo su preventivo/ordine ma non è stato fatturato, qui non apparirà."
            )

        agg = {}
        problems = []

        for conai_aml in conai_amls:
            move = conai_aml.move_id
            partner = move.partner_id

            base_product = self._find_base_product_from_conai_line(conai_aml, conai_product)
            if not base_product:
                problems.append(
                    f"- Fattura {move.name} (id {move.id}) riga CONAI id {conai_aml.id}: "
                    f"impossibile risalire al prodotto origine (manca sale_line_ids e parsing name fallito)."
                )
                continue

            # fascia su prodotto origine
            if CONAI_M2O_FIELD not in base_product._fields:
                problems.append(
                    f"- Prodotto {base_product.display_name} (id {base_product.id}) non ha il campo {CONAI_M2O_FIELD}."
                )
                continue

            fascia = base_product[CONAI_M2O_FIELD]
            if not fascia:
                problems.append(
                    f"- Prodotto {base_product.display_name} (id {base_product.id}) senza fascia CONAI."
                )
                continue

            # tariffa fascia
            if TARIFFA_TON_FIELD not in fascia._fields:
                problems.append(
                    f"- Fascia {fascia.display_name} (id {fascia.id}) senza campo {TARIFFA_TON_FIELD}."
                )
                continue

            tariffa_ton = fascia[TARIFFA_TON_FIELD] or 0.0
            if not tariffa_ton:
                problems.append(
                    f"- Fascia {fascia.display_name} (id {fascia.id}) tariffa €/ton nulla."
                )
                continue

            tariffa_kg = tariffa_ton / 1000.0

            # IMPORTO REALE FATTURATO (tax excluded) -> è quello che vuoi far tornare 1:1
            amount = conai_aml.price_subtotal  # in valuta fattura, con segno corretto
            if not amount:
                continue

            # kg assoggettati derivati dall'importo fatturato e tariffa (coerente con importi)
            kg_assogg = amount / tariffa_kg if tariffa_kg else 0.0

            # esenzione% (ATTENZIONE: è "attuale" del partner; se cambia nel tempo e non è storicizzata, può variare)
            esenzione_pct = 0.0
            if partner and (ESENZIONE_PCT_FIELD in partner._fields):
                esenzione_pct = partner[ESENZIONE_PCT_FIELD] or 0.0
            esenzione_pct = max(0.0, min(100.0, esenzione_pct))
            fattore = 1.0 - (esenzione_pct / 100.0)

            # ricostruisco kg lordi ed esenti in modo coerente con amount
            if fattore > 0:
                kg_conai = kg_assogg / fattore
            else:
                # esenzione 100% ma importo != 0 è incoerente: segnalo
                problems.append(
                    f"- Partner {partner.display_name}: esenzione 100% ma riga CONAI fatturata {amount} su {move.name}."
                )
                continue

            kg_esente = kg_conai - kg_assogg

            key = (fascia.id, partner.id)
            if key not in agg:
                agg[key] = {
                    "fascia_id": fascia.id,
                    "partner_id": partner.id,
                    "kg_conai": 0.0,
                    "kg_esenzione": 0.0,
                    "kg_assoggettati": 0.0,
                    "amount": 0.0,
                }

            agg[key]["kg_conai"] += kg_conai
            agg[key]["kg_esenzione"] += kg_esente
            agg[key]["kg_assoggettati"] += kg_assogg
            agg[key]["amount"] += amount

        if problems:
            # Mostro solo i primi per non intasare
            msg = "Alcune righe CONAI non sono state agganciate alla fascia/prodotto:\n\n" + "\n".join(problems[:10])
            if len(problems) > 10:
                msg += f"\n\n(+ altre {len(problems) - 10} righe)"
            raise UserError(msg)

        if not agg:
            raise UserError("Nessun dato aggregato: verificare fascia/tariffe e collegamenti alle righe ordine.")

        vals_list = []
        for v in agg.values():
            vals_list.append({
                "wizard_id": self.id,
                "fascia_id": v["fascia_id"],
                "partner_id": v["partner_id"],
                "kg_conai": float_round(v["kg_conai"], precision_digits=3),
                "kg_esenzione": float_round(v["kg_esenzione"], precision_digits=3),
                "kg_assoggettati": float_round(v["kg_assoggettati"], precision_digits=3),
                "amount": float_round(v["amount"], precision_digits=2),
            })

        created = self.env["conai.kg.report.line"].create(vals_list)
        _logger.info("CONAI_REPORT_INV: created lines=%s", len(created))

        return {
            "type": "ir.actions.act_window",
            "name": "Report CONAI (fatturato)",
            "res_model": "conai.kg.report.line",
            "view_mode": "list,pivot",
            "target": "current",
            "domain": [("wizard_id", "=", self.id)],
            "context": {"group_by": ["fascia_id"]},
        }