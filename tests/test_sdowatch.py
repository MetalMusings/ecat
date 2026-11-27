"""SDO values watched live (ecatsetup/sdowatch.py): reading rounds, values, bits,
changes; no Qt."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import sdowatch, tool  # noqa: E402


class TestWatch(unittest.TestCase):
    def test_values_and_bits(self):
        self.assertEqual(sdowatch.parse("0x00130004 1245188"), (0x130004, "0x00130004 1245188"))
        # a signed value: its bits are the slave's, from the hex part
        self.assertEqual(sdowatch.parse("0xffff -1")[0], 0xFFFF)
        self.assertEqual(sdowatch.parse("42"), (42, "42"))
        self.assertEqual(sdowatch.parse("ECT60"), (None, "ECT60"))
        self.assertEqual(sdowatch.bits_on(0x130004), "2, 16, 17, 20")
        self.assertEqual(sdowatch.bits_on(0), "")
        self.assertEqual(sdowatch.bits_on(None), "")
        e = sdowatch.Entry(0, 0x60FD, 0, text="0x00030000 196608")
        self.assertEqual(sdowatch.shown(e, True), "0x00030000")
        self.assertEqual(sdowatch.shown(e, False), "196608")
        e.error = "No such object"
        self.assertEqual(sdowatch.shown(e, True), "–")

    def test_rounds(self):
        w = sdowatch.Watch()
        self.assertTrue(w.add(0, 0x60FD, 0))
        self.assertTrue(w.add(1, 0x2008, 0, "uint16"))
        self.assertTrue(w.add(0, 0x60FD, 0))            # once
        self.assertEqual(len(w.entries), 2)
        values = {(0, 0x60FD, 0): "0x00030000 196608"}
        asked = []

        def read(pos, index, sub, dtype):
            asked.append(dtype)
            if (pos, index, sub) not in values:
                raise tool.ToolError("SDO upload aborted\nNo such object")
            return values[(pos, index, sub)]
        self.assertEqual(w.take(w.read_all(read), now=10.0), [])     # the first read is no change
        self.assertEqual(asked, [None, "uint16"])
        first, second = w.entries
        self.assertEqual((first.value, first.error), (0x30000, ""))
        self.assertEqual(second.error, "No such object")
        values[(0, 0x60FD, 0)] = "0x00130004 1245188"
        self.assertEqual(w.take(w.read_all(read), now=11.0), [(0, 0x60FD, 0)])
        self.assertEqual((first.value, first.changed), (0x130004, 11.0))
        self.assertEqual(w.take(w.read_all(read), now=12.0), [])     # the same again
        self.assertEqual(first.changed, 11.0)
        # a row removed while its round was out: the result is dropped
        results = w.read_all(read)
        w.remove((0, 0x60FD, 0))
        self.assertEqual(w.take(results), [])
        self.assertEqual([e.key for e in w.entries], [(1, 0x2008, 0)])

    def test_full(self):
        w = sdowatch.Watch()
        for i in range(sdowatch.MAX):
            self.assertTrue(w.add(0, 0x2000 + i, 0))
        self.assertFalse(w.add(0, 0x3000, 0))


if __name__ == "__main__":
    unittest.main()
