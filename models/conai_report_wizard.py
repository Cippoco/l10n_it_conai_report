import logging
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

    def action_generate(self):
        self.ensure_one()

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("Intervallo date non valido: 'Dal' è successivo a 'Al'.")

        # ====== CONFIG ======
        ESENZIONE_PCT_FIELD = "x_studio_esenzione_conai_percentuale"  # su res.partner
        CONAI_M2O_FIELD = "x_studio_fascia_conai"                     # su product.product -> x_conai
        TARIFFA_TON_FIELD = "x_studio_tariffa_ton"                    # su x_conai
        CONAI_DEFAULT_CODE = "CONAI"                                  # prodotto CONAI (da escludere)
        # ====================

        # pulizia righe precedenti (per questo wizard)
        self.env["conai.kg.report.line"].search([("wizard_id", "=", self.id)]).unlink()

        conai_product = self.env["product.product"].search([("default_code", "=", CONAI_DEFAULT_CODE)], limit=1)
        _logger.info(
            "CONAI_REPORT: start company=%s date_from=%s date_to=%s conai_product_id=%s",
            self.company_id.id,
            self.date_from,
            self.date_to,
            conai_product.id if conai_product else None
        )

        # Fatture postate nel periodo usando invoice_date OR date (accounting date)
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
                "Nota: il report filtra per Invoice Date oppure Accounting Date (date).\n"
                "Se le fatture sono state inviate di recente ma hanno data contabile precedente, allarga il range."
            )

        # Righe fattura: FIX Odoo18 -> NON filtrare display_type a False (può essere 'product')
        Line = self.env["account.move.line"]
        line_domain = [
            ("move_id", "in", moves.ids),
            ("product_id", "!=", False),
        ]
        # opzionale: escludi righe tecniche non in tab fattura, se il campo esiste
        if "exclude_from_invoice_tab" in Line._fields:
            line_domain.append(("exclude_from_invoice_tab", "=", False))

        if conai_product:
            line_domain.append(("product_id", "!=", conai_product.id))

        amls = Line.search(line_domain)
        _logger.info("CONAI_REPORT: invoice lines (product) found=%s", len(amls))

        if not amls:
            # log extra per capire se il problema è solo display_type o product_id assente
            all_lines = Line.search([("move_id", "in", moves.ids)])
            _logger.info(
                "CONAI_REPORT: DEBUG total move lines=%s, with product=%s",
                len(all_lines),
                len(all_lines.filtered(lambda l: bool(l.product_id)))
            )
            raise UserError(
                "Fatture trovate, ma nessuna riga prodotto utile.\n"
                "Possibili cause:\n"
                "- le righe fattura non hanno prodotto (product_id vuoto)\n"
                "- oppure sono solo righe CONAI (escluse dal report)\n"
            )

        # Aggrego per (fascia_id, partner_id)
        agg = {}
        skipped = {
            "section_or_note": 0,
            "no_conai_field": 0,
            "no_fascia": 0,
            "no_tariff_field": 0,
            "no_tariff": 0,
            "no_qty": 0,
            "no_weight": 0,
        }

        for line in amls:
            # skip note/section (in Odoo18 le righe prodotto possono essere display_type='product')
            if line.display_type in ("line_section", "line_note"):
                skipped["section_or_note"] += 1
                continue

            move = line.move_id
            partner = move.partner_id
            product = line.product_id

            # fascia su prodotto
            if CONAI_M2O_FIELD not in product._fields:
                skipped["no_conai_field"] += 1
                continue
            fascia = product[CONAI_M2O_FIELD]
            if not fascia:
                skipped["no_fascia"] += 1
                continue

            # tariffa €/ton su fascia
            if TARIFFA_TON_FIELD not in fascia._fields:
                skipped["no_tariff_field"] += 1
                continue
            tariffa_ton = fascia[TARIFFA_TON_FIELD] or 0.0
            if not tariffa_ton:
                skipped["no_tariff"] += 1
                continue
            tariffa_kg = tariffa_ton / 1000.0

            # qty (convertita in UoM prodotto)
            qty = line.quantity or 0.0
            if qty and getattr(line, "product_uom_id", False) and line.product_uom_id and product.uom_id:
                qty = line.product_uom_id._compute_quantity(qty, product.uom_id)
            if not qty:
                skipped["no_qty"] += 1
                continue

            # peso unitario: variante -> fallback template
            peso_unit_kg = product.weight or product.product_tmpl_id.weight or 0.0
            if not peso_unit_kg:
                skipped["no_weight"] += 1
                continue

            # note credito negative
            sign = -1.0 if move.move_type == "out_refund" else 1.0
            kg_lordi = sign * (qty * peso_unit_kg)
            if not kg_lordi:
                skipped["no_qty"] += 1
                continue

            # esenzione % cliente
            esenzione_pct = 0.0
            if partner and (ESENZIONE_PCT_FIELD in partner._fields):
                esenzione_pct = partner[ESENZIONE_PCT_FIELD] or 0.0
            esenzione_pct = max(0.0, min(100.0, esenzione_pct))

            kg_esente = kg_lordi * (esenzione_pct / 100.0)
            kg_assogg = kg_lordi - kg_esente
            amount = kg_assogg * tariffa_kg

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

            agg[key]["kg_conai"] += kg_lordi
            agg[key]["kg_esenzione"] += kg_esente
            agg[key]["kg_assoggettati"] += kg_assogg
            agg[key]["amount"] += amount

        _logger.info("CONAI_REPORT: agg keys=%s skipped=%s", len(agg), skipped)

        if not agg:
            raise UserError(
                "Nessun dato CONAI calcolabile nel periodo.\n\n"
                f"Righe fattura analizzate: {len(amls)}\n"
                f"Skip: section/note={skipped['section_or_note']}, "
                f"no campo fascia={skipped['no_conai_field']}, no fascia={skipped['no_fascia']}, "
                f"no campo tariffa={skipped['no_tariff_field']}, tariffa=0={skipped['no_tariff']}, "
                f"qty=0={skipped['no_qty']}, peso=0={skipped['no_weight']}\n\n"
                "Controlla: prodotti con fascia CONAI valorizzata, tariffa fascia valorizzata, peso prodotto."
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

        return {
            "type": "ir.actions.act_window",
            "name": "Report CONAI Kg",
            "res_model": "conai.kg.report.line",
            "view_mode": "list,pivot",
            "target": "current",
            "domain": [("wizard_id", "=", self.id)],
            "context": {"group_by": ["fascia_id"]},
        }