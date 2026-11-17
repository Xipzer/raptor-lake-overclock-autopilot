import unittest

import fakes
from rlh import scewin


class ScewinParser(unittest.TestCase):
    def setUp(self):
        self.text = fakes.export()
        self.sc = scewin.Script(self.text)

    def test_round_trip_is_byte_exact(self):
        self.assertEqual(self.sc.text(), self.text)

    def test_names_strip_markup_and_live_value(self):
        q = self.sc.find("CAS# Latency (tCL)")
        self.assertEqual(q.name, "CAS# Latency (tCL)")
        self.assertEqual(q.token, "2CA5")
        self.assertEqual(self.sc.find("VDD_IMC Voltage").token, "2CDB")
        self.assertEqual(self.sc.find("fixed voltage(v)").token, "2CEF")  # case-insensitive

    def test_live_value_and_stored_value(self):
        q = self.sc.find("Refresh Cycle Time (tRFC)")
        self.assertEqual(self.sc.label(q), "680")
        self.assertEqual(self.sc.value(q), 0)  # Auto
        self.assertEqual(self.sc.label(self.sc.find("VDD_IMC Voltage")), "1.450V")

    def test_inactive_blocks_are_ignored(self):
        self.assertEqual(len(self.sc.matches("Boot Failure Guard Message")), 1)

    def test_duplicate_names_need_index_or_token(self):
        with self.assertRaises(scewin.QuestionError) as e:
            self.sc.find("tRDRD_sg")
        self.assertIn("ambiguous", str(e.exception))
        self.assertEqual(self.sc.find("tRDRD_sg", index=1).token, "2CC9")
        self.assertEqual(self.sc.find("tRDRD_sg", token="2cc9").token, "2CC9")
        with self.assertRaises(scewin.QuestionError):
            self.sc.find("tRDRD_sg", index=5)
        with self.assertRaises(scewin.QuestionError):
            self.sc.find("No Such Question")

    def test_set_numeric_keeps_layout(self):
        q = self.sc.find("CAS# Latency (tCL)")
        self.sc.set(q, 38)
        self.assertEqual(self.sc.value(q), 38)
        again = scewin.Script(self.sc.text())
        self.assertEqual(again.value(again.find("CAS# Latency (tCL)")), 38)
        self.assertEqual(len(again.questions), len(self.sc.questions))
        self.assertIn("\r\n", again.text())

    def test_set_plain_value_without_brackets(self):
        q = self.sc.find("Reset NVRAM")
        self.assertEqual(self.sc.value(q), 1)
        self.sc.set(q, 0)
        self.assertEqual(self.sc.value(q), 0)
        self.assertIn("// Enabled = 1", self.sc.lines[q._field("Value")])

    def test_options(self):
        q = self.sc.find("Load XMP Setting")
        self.assertEqual(self.sc.options(q), ["Auto", "XMP 3.0 Profile 1", "XMP 3.0 Profile 2"])
        self.assertEqual(self.sc.value(q), "XMP 3.0 Profile 1")
        self.sc.set(q, "auto")
        self.assertEqual(self.sc.value(q), "Auto")
        self.assertEqual(sum(1 for l in q.lines if "*[" in self.sc.lines[l]), 1)
        self.sc.set(q, "02")
        self.assertEqual(self.sc.value(q), "XMP 3.0 Profile 2")
        with self.assertRaises(ValueError):
            self.sc.set(q, "XMP 9")

    def test_string_question_is_not_settable_as_number(self):
        q = self.sc.find("Some Password")
        self.assertEqual(self.sc.kind(q), "string")
        self.assertEqual(self.sc.value(q), "")
        with self.assertRaises(ValueError):
            self.sc.set(q, 1)

    def test_set_label_rewrites_live_value_only(self):
        q = self.sc.find("CAS# Latency (tCL)")
        self.sc.set_label(q, "38")
        self.assertEqual(self.sc.label(q), "38")
        self.assertEqual(q.name, "CAS# Latency (tCL)")
        with self.assertRaises(ValueError):
            self.sc.set_label(self.sc.find("Fixed Voltage(V)"), "1.3")

    def test_header_and_search(self):
        self.assertEqual(self.sc.header("HIICrc32"), "0000BEEF")
        self.assertEqual({q.token for q in self.sc.search(r"^Refresh Cycle")}, {"2CAC", "2CAE"})

    def test_unmatched_lines(self):
        out = "Password accepted\nWARNING: unmatched question tCL 40\nimported successfully"
        self.assertEqual(scewin.unmatched(out), ["WARNING: unmatched question tCL 40"])


if __name__ == "__main__":
    unittest.main()
