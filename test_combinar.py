"""Pruebas de la regla de combinación de direcciones (combinar / _combinar_baja).

    python3 test_combinar.py
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_circle_brevo as S

class Combinar(unittest.TestCase):
    def test_contadores_se_suman(self):
        r = S.combinar({"brevo_enviados": 10, "brevo_aperturas": 4}, {"brevo_enviados": 5, "brevo_aperturas": 1})
        self.assertEqual((r["brevo_enviados"], r["brevo_aperturas"]), (15, 5))
    def test_tasa_se_recalcula(self):
        r = S.combinar({"brevo_enviados": 10, "brevo_aperturas": 4, "brevo_tasa_apertura": 40},
                       {"brevo_enviados": 10, "brevo_aperturas": 6, "brevo_tasa_apertura": 60})
        self.assertEqual(r["brevo_tasa_apertura"], 50.0)
    def test_fechas_maximo(self):
        r = S.combinar({"brevo_ultima_apertura": "2026-08-01"}, {"brevo_ultima_apertura": "2026-09-15"})
        self.assertEqual(r["brevo_ultima_apertura"], "2026-09-15")
    def test_nivel_maximo(self):
        self.assertEqual(S.combinar({"circle_nivel": 3}, {"circle_nivel": 1})["circle_nivel"], 3)
    def test_alta_minimo(self):
        r = S.combinar({"circle_miembro_desde": "2026-10-06"}, {"circle_miembro_desde": "2026-01-02"})
        self.assertEqual(r["circle_miembro_desde"], "2026-01-02")
    def test_none_no_pisa(self):
        self.assertEqual(S.combinar({"brevo_clics": 2}, {"brevo_clics": None})["brevo_clics"], 2)
    def test_baja_usuario_gana(self):
        r = S.combinar({"brevo_desuscrito": "false"},
                       {"brevo_desuscrito": "true", "brevo_desuscripcion_tipo": "usuario", "brevo_fecha_desuscripcion": "2026-05-05"})
        self.assertEqual((r["brevo_desuscrito"], r["brevo_desuscripcion_tipo"]), ("true", "usuario"))
    def test_rebote_de_alias_no_suprime(self):
        r = S.combinar({"brevo_desuscrito": "false"},
                       {"brevo_desuscrito": "true", "brevo_desuscripcion_tipo": "rebote_duro"})
        self.assertEqual(r["brevo_desuscrito"], "false")
        self.assertNotIn("brevo_desuscripcion_tipo", r)       # coherencia: sin baja, sin tipo
    def test_admin_de_alias_no_suprime(self):
        r = S.combinar({"brevo_desuscrito": "true", "brevo_desuscripcion_tipo": "admin"}, {"brevo_desuscrito": "false"})
        self.assertEqual(r["brevo_desuscrito"], "true")      # gana la primaria si ninguna es de usuario
    def test_dos_bajas_usuario_fecha_minima(self):
        r = S.combinar({"brevo_desuscrito": "true", "brevo_desuscripcion_tipo": "usuario", "brevo_fecha_desuscripcion": "2026-06-01"},
                       {"brevo_desuscrito": "true", "brevo_desuscripcion_tipo": "usuario", "brevo_fecha_desuscripcion": "2026-03-01"})
        self.assertEqual(r["brevo_fecha_desuscripcion"], "2026-03-01")
    def test_identidad_no_se_suma(self):
        r = S.combinar({"es_comunidad": "true", "firstname": "Ana"}, {"es_comunidad": "true", "firstname": "Anita"})
        self.assertEqual(r["firstname"], "Ana")
    def test_internas_circle_excluidas(self):
        self.assertTrue({"_c_firstname", "_c_lastname", "_c_company", "_c_jobtitle", "_c_rol"} <= S.INTERNAS)

if __name__ == "__main__":
    unittest.main(verbosity=1)
