"""Small offline spreadsheet playbooks injected only when relevant."""

SKILLS = {
    "filter": """Filtering checklist: use inclusive boundaries when requested; preserve source order; copy requested styles and row dimensions; clear trailing destination cells; verify count plus first/last output row.""",
    "sort": """Sorting checklist: identify headers separately; sort only data rows; make every stated key explicit in the tuple; verify adjacent output rows satisfy the key order.""",
    "aggregate": """Aggregation checklist: define grouping keys exactly; aggregate before numbering; sort groups after aggregation; write totals as formulas only when requested and verify their ranges.""",
    "delete": """Deletion/compaction checklist: collect kept rows before writing; preserve their relative order; clear trailing cells; do not move unrelated columns unless the instruction explicitly says to delete entire rows.""",
    "conditional": """Conditional-edit checklist: enumerate all conditions, test both matching and non-matching examples, and verify only intended cells/ranges changed.""",
}


def selected_skills(instruction: str) -> str:
    text = instruction.lower()
    chosen = []
    if any(word in text for word in ("filter", "criteria", "start date", "end date")):
        chosen.append(SKILLS["filter"])
    if any(word in text for word in ("sort", "lowest to highest", "ascending", "alphabetical")):
        chosen.append(SKILLS["sort"])
    if any(word in text for word in ("sum", "total", "aggregate", "group")):
        chosen.append(SKILLS["aggregate"])
    if any(word in text for word in ("delete", "remove", "shift cells up", "duplicates")):
        chosen.append(SKILLS["delete"])
    if any(word in text for word in ("if ", "when ", "condition", "highlighted")):
        chosen.append(SKILLS["conditional"])
    return "\n".join(f"- {skill}" for skill in chosen)
