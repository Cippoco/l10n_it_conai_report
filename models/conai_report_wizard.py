import logging
import re
from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_round

_logger = logging.getLogger(__name__)


class ConaiKgReportWizard(models.TransientModel):
    _name = "conai.kg.report.wizard"
    _description = "Wizard Report CONAI Kg per Fascia/Cliente"

    date_from = fields.Date(required=True, default=fields.Date.context_today)
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)

    def _find_base_product_from_conai_line(self, conai_aml, conai_product):
        """
        Vecchia logica:
        1) sale_line_ids -> order_line precedente
        2) fallback parsing del nome riga CONAI
        """
        if 'sale_line_ids' in conai_aml._fields and conai_aml.sale_line_ids:
            sl = conai_aml.sale_line_ids[0]
            if 'order_id' in sl._fields and sl.order_id:
                order = sl.order_id
                conai_seq = sl.sequence or 0
                candidates = order.order_line.filtered(
                    lambda l: (not l.display_type)
                    and l.product_id
                    and (l.product_id != conai_product)
                    and ((l.sequence or 0) < conai_seq)
                ).sorted(key=lambda l: (l.sequence or 0, l.id), reverse=True)

                if candidates:
                    return candidates[0].product_id

        name = (conai_aml.name or "").strip()
        if name:
            first_line = name.splitlines()[0].strip()
            prefix = "Contributo ambientale CONAI per "
            if first_line.startswith(prefix):
                prod_txt = first_line[len(prefix):].strip()
            else:
                m = re.search(r"\bper\s+(.*)$", first_line)
                prod_txt = m.group(1).strip() if m else ""

            if prod_txt:
                res = self.env["product.product"].name_search(prod_txt, operator="ilike", limit=5)
                if res:
                    return self.env["product.product"].browse(res[0][0])

        return False

    def action_generate(self):
        self.ensure_one()

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("Intervallo date non valido: 'Dal' è successivo a 'Al'.")

        CONAI_DEFAULT_CODE = "CONAI"
        CONAI_M2O_FIELD = "x_studio_fascia_conai"
        TARIFFA_TON_FIELD = "x_studio_tariffa_ton"

        SNAP_FASCIA_FIELD = "x_studio_conai_fascia_snapshot_id"
        SNAP_TARIFFA_TON_FIELD = "x_studio_conai_tariffa_ton_snapshot"
        SNAP_ESENZIONE_PCT_FIELD = "x_studio_conai_esenzione_pct_snapshot"

        GO_LIVE_DATE = fields.Date.to_date("2026-03-19")

        self.env["conai.kg.report.line"].search([("wizard_id", "=", self.id)]).unlink()

        conai_product = self.env["product.product"].search([("default_code", "=", CONAI_DEFAULT_CODE)], limit=1)
        if not conai_product:
            raise UserError("Prodotto CONAI non trovato (default_code='CONAI').")

        _logger.info(
            "CONAI_REPORT: start company=%s date_from=%s date_to=%s conai_product_id=%s",
            self.company_id.id, self.date_from, self.date_to, conai_product.id
        )

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
        _logger.info("CONAI_REPORT: moves found=%s", len(moves))

        if not moves:
            raise UserError(
                "Nessuna fattura/NC POSTATA trovata nel periodo.\n"
                "Il report filtra per Invoice Date oppure Accounting Date."
            )

        Line = self.env["account.move.line"]
        line_domain = [
            ("move_id", "in", moves.ids),
            ("product_id", "=", conai_product.id),
        ]
        if "exclude_from_invoice_tab" in Line._fields:
            line_domain.append(("exclude_from_invoice_tab", "=", False))

        conai_lines = Line.search(line_domain)
        _logger.info("CONAI_REPORT: conai invoice lines found=%s", len(conai_lines))

        if not conai_lines:
            raise UserError("Nel periodo non risultano righe fattura con prodotto CONAI.")

        agg = {}
        problems = []
        used_new = 0
        used_old = 0

        for line in conai_lines:
            move = line.move_id
            partner = move.partner_id
            doc_date = move.invoice_date or move.date

            amount = line.price_subtotal or 0.0
            if not amount:
                continue

            use_new_logic = False
            if doc_date and doc_date >= GO_LIVE_DATE:
                has_fascia = (SNAP_FASCIA_FIELD in line._fields) and bool(line[SNAP_FASCIA_FIELD])
                has_tariffa = (SNAP_TARIFFA_TON_FIELD in line._fields) and bool(line[SNAP_TARIFFA_TON_FIELD])
                has_esenzione_field = SNAP_ESENZIONE_PCT_FIELD in line._fields
                if has_fascia and has_tariffa and has_esenzione_field:
                    use_new_logic = True

            if use_new_logic:
                fascia = line[SNAP_FASCIA_FIELD]
                tariffa_ton = line[SNAP_TARIFFA_TON_FIELD] or 0.0
                esenzione_pct = line[SNAP_ESENZIONE_PCT_FIELD] or 0.0
                esenzione_pct = max(0.0, min(100.0, esenzione_pct))

                tariffa_kg = tariffa_ton / 1000.0
                if not tariffa_kg:
                    problems.append(
                        "- %s riga CONAI id %s: tariffa snapshot nulla" % (move.name, line.id)
                    )
                    continue

                kg_assogg = amount / tariffa_kg
                fattore = 1.0 - (esenzione_pct / 100.0)

                if fattore > 0:
                    kg_conai = kg_assogg / fattore
                    kg_esenzione = kg_conai - kg_assogg
                else:
                    # caso incoerente: esenzione 100% ma importo presente
                    kg_conai = kg_assogg
                    kg_esenzione = 0.0

                used_new += 1

            else:
                # vecchia logica
                base_product = self._find_base_product_from_conai_line(line, conai_product)
                if not base_product:
                    problems.append(
                        "- Fattura %s (id %s) riga CONAI id %s: impossibile risalire al prodotto origine "
                        "(manca sale_line_ids e parsing name fallito)." % (move.name, move.id, line.id)
                    )
                    continue

                if CONAI_M2O_FIELD not in base_product._fields:
                    problems.append(
                        "- Fattura %s riga CONAI id %s: prodotto %s senza campo fascia CONAI." %
                        (move.name, line.id, base_product.display_name)
                    )
                    continue

                fascia = base_product[CONAI_M2O_FIELD]
                if not fascia:
                    problems.append(
                        "- Fattura %s riga CONAI id %s: prodotto %s senza fascia CONAI." %
                        (move.name, line.id, base_product.display_name)
                    )
                    continue

                if TARIFFA_TON_FIELD not in fascia._fields:
                    problems.append(
                        "- Fattura %s riga CONAI id %s: fascia %s senza campo tariffa." %
                        (move.name, line.id, fascia.display_name)
                    )
                    continue

                tariffa_ton = fascia[TARIFFA_TON_FIELD] or 0.0
                if not tariffa_ton:
                    problems.append(
                        "- Fattura %s riga CONAI id %s: fascia %s con tariffa nulla." %
                        (move.name, line.id, fascia.display_name)
                    )
                    continue

                esenzione_pct = 0.0
                if partner and ("x_studio_esenzione_conai_percentuale" in partner._fields):
                    esenzione_pct = partner["x_studio_esenzione_conai_percentuale"] or 0.0
                esenzione_pct = max(0.0, min(100.0, esenzione_pct))

                tariffa_kg = tariffa_ton / 1000.0
                kg_assogg = amount / tariffa_kg
                fattore = 1.0 - (esenzione_pct / 100.0)

                if fattore > 0:
                    kg_conai = kg_assogg / fattore
                    kg_esenzione = kg_conai - kg_assogg
                else:
                    kg_conai = kg_assogg
                    kg_esenzione = 0.0

                used_old += 1

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
            agg[key]["kg_esenzione"] += kg_esenzione
            agg[key]["kg_assoggettati"] += kg_assogg
            agg[key]["amount"] += amount

        _logger.info(
            "CONAI_REPORT: agg keys=%s used_new=%s used_old=%s problems=%s",
            len(agg), used_new, used_old, len(problems)
        )

        if not agg:
            raise UserError(
                "Nessun dato aggregato disponibile.\n\n" +
                ("\n".join(problems[:10]) if problems else "")
            )

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
        _logger.info("CONAI_REPORT: created lines=%s", len(created))

        # se vuoi puoi lasciare solo log; io non blocco il report se alcune righe vecchie falliscono
        if problems:
            _logger.warning("CONAI_REPORT: problemi su alcune righe:\n%s", "\n".join(problems[:20]))

        return {
            "type": "ir.actions.act_window",
            "name": "Report CONAI Kg",
            "res_model": "conai.kg.report.line",
            "view_mode": "list,pivot",
            "target": "current",
            "domain": [("wizard_id", "=", self.id)],
            "context": {"group_by": ["fascia_id"]},
        }