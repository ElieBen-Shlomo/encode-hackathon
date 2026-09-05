import os

import openpyxl

OUT = os.environ["OUT_XLSX"]

wb = openpyxl.load_workbook(OUT)
ws = wb["Sheet1"]

# Sum the B2:M16 grid where the row date (col A) is within P6..Q6 and the
# column time (row 1) is within R3..R4. The date mask is a column vector and the
# time mask a row vector, so SUMPRODUCT broadcasts them into the grid's shape.
ws["R6"] = ("=SUMPRODUCT((A2:A16>=P6)*(A2:A16<=Q6)"
            "*(B1:M1>=R3)*(B1:M1<=R4)*B2:M16)")

wb.save(OUT)
