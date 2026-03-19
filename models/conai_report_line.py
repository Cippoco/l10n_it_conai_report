from odoo import fields, models


class ConaiKgReportLine(models.TransientModel):
    _name = "conai.kg.report.line"
    _description = "Riga Report CONAI Kg"

    wizard_id = fields.Many2one("conai.kg.report.wizard", required=True, ondelete="cascade")

    fascia_id = fields.Many2one("x_conai", string="Fascia CONAI", required=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True)

    kg_conai = fields.Float(string="Kg CONAI", digits=(16, 3))
    kg_esenzione = fields.Float(string="Kg Esenzione", digits=(16, 3))
    kg_assoggettati = fields.Float(string="Kg Assoggettati", digits=(16, 3))

    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    amount = fields.Monetary(string="Tot Importo", currency_field="currency_id")