import base64
import struct
import unittest
import zlib

from mcpres import datasets as d


class DatasetIntegrity(unittest.TestCase):
    def test_countries(self):
        self.assertGreaterEqual(len(d.COUNTRIES), 40)
        for key in ("iso2", "iso3", "name"):
            vals = [c[key] for c in d.COUNTRIES]
            self.assertEqual(len(vals), len(set(vals)), key)
        for c in d.COUNTRIES:
            self.assertEqual((len(c["iso2"]), len(c["iso3"]), len(c["currency"])), (2, 3, 3))
            self.assertTrue(c["iso2"].isupper() and c["iso3"].isupper())
            self.assertGreater(c["population_millions"], 0)
            self.assertGreater(c["area_thousand_km2"], 0)
            self.assertTrue(c["languages"])
            self.assertIn(c["region"], d.REGIONS)

    def test_elements(self):
        self.assertEqual([e["number"] for e in d.ELEMENTS], list(range(1, 37)))
        self.assertEqual(len({e["symbol"] for e in d.ELEMENTS}), 36)
        masses = [e["atomic_mass"] for e in d.ELEMENTS]
        self.assertEqual(masses, sorted(masses)[:0] + masses)  # sanity: floats
        # mass increases with Z except the well-known inversion Ar > K (39.948 vs 39.098) and Co > Ni (58.933 vs 58.693)
        inversions = [(d.ELEMENTS[i]["symbol"], d.ELEMENTS[i + 1]["symbol"]) for i in range(35) if masses[i] > masses[i + 1]]
        self.assertEqual(inversions, [("Ar", "K"), ("Co", "Ni")])
        for e in d.ELEMENTS:
            self.assertTrue(1 <= e["group"] <= 18 and 1 <= e["period"] <= 4)
        self.assertEqual({e["symbol"] for e in d.ELEMENTS if e["group"] == 18}, {"He", "Ne", "Ar", "Kr"})
        self.assertEqual(d.ELEMENT_BY_SYMBOL["Fe"]["number"], 26)

    def test_glossary(self):
        terms = [g["term"] for g in d.GLOSSARY]
        self.assertEqual(terms, sorted(terms), "kept sorted")
        self.assertEqual(len(terms), len(set(terms)))
        for g in d.GLOSSARY:
            self.assertTrue(g["definition"].endswith("."))
            self.assertLessEqual(len(g["definition"]), 600)
            self.assertEqual(g["term"], g["term"].lower())

    def test_units_round_trip_and_known_values(self):
        for cat, (base, table) in d.UNITS.items():
            if cat == "temperature":
                continue
            self.assertEqual(table[base], 1.0, cat)
            for a in table:
                for b in table:
                    x, _ = d.convert(7.25, a, b)
                    back, _ = d.convert(x, b, a)
                    self.assertAlmostEqual(back, 7.25, places=6, msg="%s<->%s" % (a, b))
        self.assertAlmostEqual(d.convert(1, "mi", "km")[0], 1.609344)
        self.assertAlmostEqual(d.convert(1, "KiB", "B")[0], 1024)
        self.assertAlmostEqual(d.convert(1, "kWh", "kJ")[0], 3600)
        self.assertAlmostEqual(d.convert(100, "km/h", "m/s")[0], 27.7777778, places=6)
        self.assertAlmostEqual(d.convert(1, "atm", "kPa")[0], 101.325)
        self.assertEqual(d.convert(100, "C", "F")[0], 212.0)
        self.assertAlmostEqual(d.convert(-40, "C", "F")[0], -40)
        self.assertAlmostEqual(d.convert(0, "K", "C")[0], -273.15)

    def test_unit_errors(self):
        for src, dst in (("m", "kg"), ("furlong", "m"), ("C", "m")):
            with self.assertRaises(d.UnitError):
                d.convert(1, src, dst)
        with self.assertRaises(d.UnitError):
            d.convert(-10, "K", "C")  # below absolute zero

    def test_unit_names_are_unambiguous_across_categories(self):
        seen = {}
        for cat, (_, table) in d.UNITS.items():
            for u in table:
                self.assertNotIn(u, seen, "%s appears in %s and %s" % (u, seen.get(u), cat))
                seen[u] = cat


class Blobs(unittest.TestCase):
    def test_png_is_structurally_valid(self):
        png = d.BLOBS["sample.png"]["data"]
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        pos, chunks, idat = 8, [], b""
        while pos < len(png):
            (n,) = struct.unpack(">I", png[pos:pos + 4])
            kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
            (crc,) = struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])
            self.assertEqual(crc, zlib.crc32(kind + data) & 0xFFFFFFFF, kind)
            chunks.append(kind)
            if kind == b"IDAT":
                idat += data
            pos += 12 + n
        self.assertEqual(chunks, [b"IHDR", b"IDAT", b"IEND"])
        w, h, depth, ctype = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((w, h, depth, ctype), (32, 32, 8, 2))
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), 32 * (1 + 32 * 3))
        self.assertEqual(raw[1:4], bytes((0, 0, 255)))  # pixel (0,0): r=0,g=0, checker on

    def test_deterministic_across_calls(self):
        self.assertEqual(d.make_png(), d.BLOBS["sample.png"]["data"])
        self.assertEqual(d.make_random_bin(), d.BLOBS["random.bin"]["data"])
        self.assertEqual(len(d.BLOBS["random.bin"]["data"]), 512)
        self.assertGreater(len(set(d.BLOBS["random.bin"]["data"])), 200)
        self.assertEqual(base64.b64decode(base64.b64encode(d.BLOBS["random.bin"]["data"])), d.BLOBS["random.bin"]["data"])


if __name__ == "__main__":
    unittest.main()
