import os

import openpyxl

OUT = os.environ["OUT_XLSX"]
wb = openpyxl.load_workbook(OUT)
ws = wb["Sheet1"]

# Read every word from column A.
words = []
for r in range(1, ws.max_row + 1):
    v = ws.cell(row=r, column=1).value
    if isinstance(v, str) and v.strip():
        words.append(v.strip())

# Step 1: sort column A alphabetically, in place.
words.sort()
for i, w in enumerate(words, start=1):
    ws.cell(row=i, column=1).value = w

wordset = set(words)

# A match: moving the fifth letter (index 4) to the front turns the original word
# into another listed word (EARTHPEA -> HEARTPEA). The transformation goes in C,
# the original in D; the last three letters are unchanged so both share an ending.
pairs = []  # (transformation, original)
for w in words:
    if len(w) >= 5:
        t = w[4] + w[:4] + w[5:]
        if t != w and t in wordset:
            pairs.append((t, w))

# Group by ending in the requested order, alphabetical by original within a group.
order = ["ING", "ERS", "ATE", "EST", "ONE", "IER", "ILY"]
pairs.sort(key=lambda p: (order.index(p[1][-3:]) if p[1][-3:] in order else len(order), p[1]))

# Paste the pairs at columns C and D starting on row 1, overwriting the example there.
for i, (t, w) in enumerate(pairs, start=1):
    ws.cell(row=i, column=3).value = t
    ws.cell(row=i, column=4).value = w

wb.save(OUT)
