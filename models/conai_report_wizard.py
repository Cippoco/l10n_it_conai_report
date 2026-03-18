from odoo import api, fields, models
from odoo.tools.float_utils import float_round


class ConaiKgReportWizard(models.TransientModel):
    _name = "conai.kg.report.wizard"
    _description = "Wizard Report CONAI Kg per Fascia/Cliente"

    date_from = fields.Date(required=True, default=fields.Date.context_today)
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)

    line_ids = fields.One2many("conai.kg.report.line", "wizard_id", string="Righe Report")

    def action_generate(self):
        self.ensure_one()
        self.line_ids.unlink()

        # Campi custom usati nel tuo progetto
        ESENZIONE_PCT_FIELD = "x_studio_esenzione_conai_percentuale"   # su res.partner
        CONAI_M2O_FIELD = "x_studio_fascia_conai"                      # su product.product -> x_conai
        TARIFFA_TON_FIELD = "x_studio_tariffa_ton"                     # su x_conai
        CONAI_DEFAULT_CODE = "CONAI"                                   # per escludere righe prodotto CONAI

        # prodotto CONAI (per esclusione)
        conai_product = self.env["product.product"].search([("default_code", "=", CONAI_DEFAULT_CODE)], limit=1)

        # Dominio: fatture clienti + note credito, postate, nel periodo
        domain = [
            ("move_id.state", "=", "posted"),
            ("move_id.move_type", "in", ("out_invoice", "out_refund")),
            ("move_id.company_id", "=", self.company_id.id),
            ("move_id.invoice_date", ">=", self.date_from),
            ("move_id.invoice_date", "<=", self.date_to),
            ("display_type", "=", False),
            ("product_id", "!=", False),
        ]
        if conai_product:
            domain.append(("product_id", "!=", conai_product.id))

        amls = self.env["account.move.line"].search(domain)

        # Aggrego per (fascia_id, partner_id)
        agg = {}  # key=(fascia_id, partner_id) -> totals

        for line in amls:
            move = line.move_id
            partner = move.partner_id
            product = line.product_id

            # Fascia CONAI sul prodotto
            if CONAI_M2O_FIELD not in product._fields:
                continue
            fascia = product[CONAI_M2O_FIELD]
            if not fascia:
                continue

            # Tariffa €/ton sulla fascia
            if TARIFFA_TON_FIELD not in fascia._fields:
                continue
            tariffa_ton = fascia[TARIFFA_TON_FIELD] or 0.0
            if not tariffa_ton:
                continue
            tariffa_kg = tariffa_ton / 1000.0

            # Quantità: converto nella UoM prodotto per coerenza col "peso per 1 unità prodotto"
            qty = line.quantity or 0.0
            if qty and hasattr(line, "product_uom_id") and line.product_uom_id and product.uom_id:
                qty = line.product_uom_id._compute_quantity(qty, product.uom_id)

            if not qty:
                continue

            # Peso unitario: variante -> fallback template (uniforme come CONAI)
            peso_unit_kg = product.weight or product.product_tmpl_id.weight or 0.0
            if not peso_unit_kg:
                continue

            # Note di credito: segno negativo
            sign = -1.0 if move.move_type == "out_refund" else 1.0

            kg_lordi = sign * (qty * peso_unit_kg)
            if not kg_lordi:
                continue

            # Esenzione % cliente (0..100)
            esenzione_pct = 0.0
            if partner and (ESENZIONE_PCT_FIELD in partner._fields):
                esenzione_pct = partner[ESENZIONE_PCT_FIELD] or 0.0
            esenzione_pct = max(0.0, min(100.0, esenzione_pct))

            kg_esente = kg_lordi * (esenzione_pct / 100.0)
            kg_assogg = kg_lordi - kg_esente

            # Importi (coerenti col tuo calcolo server action)
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

        # Creo righe report
        vals_list = []
        for data in agg.values():
            vals_list.append({
                "wizard_id": self.id,
                "fascia_id": data["fascia_id"],
                "partner_id": data["partner_id"],
                "kg_conai": float_round(data["kg_conai"], precision_digits=3),
                "kg_esenzione": float_round(data["kg_esenzione"], precision_digits=3),
                "kg_assoggettati": float_round(data["kg_assoggettati"], precision_digits=3),
                "amount": float_round(data["amount"], precision_digits=2),
            })

        if vals_list:
            self.env["conai.kg.report.line"].create(vals_list)

        # Riapro wizard (con tab righe)
        return {
            "type": "ir.actions.act_window",
            "name": "Report CONAI Kg",
            "res_model": "conai.kg.report.line",
            "view_mode": "list,pivot",
            "target": "current",
            "domain": [("wizard_id", "=", self.id)],
            "context": {"group_by": ["fascia_id"]},
        }


class ConaiKgReportLine(models.TransientModel):
    _name = "conai.kg.report.line"
    _description = "Riga Report CONAI Kg"

    wizard_id = fields.Many2one("conai.kg.report.wizard", required=True, ondelete="cascade")

    # x_conai è il modello della tua fascia (da commento nel codice precedente)
    fascia_id = fields.Many2one("x_conai", string="Fascia CONAI", required=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True)

    kg_conai = fields.Float(string="Kg CONAI", digits=(16, 3))
    kg_esenzione = fields.Float(string="Kg Esenzione", digits=(16, 3))
    kg_assoggettati = fields.Float(string="Kg Assoggettati", digits=(16, 3))
    amount = fields.Monetary(string="Tot Importo", currency_field="currency_id")

    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)