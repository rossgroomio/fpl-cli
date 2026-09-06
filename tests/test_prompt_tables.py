"""Structural guards over the markdown tables the LLM prompts declare.

A prompt's `<output_format>` table is a contract with the model: the header
names the columns, and the bracketed instruction underneath tells the model
what to put in them. When a column is declared but never described, the model
leaves it blank or improvises - the Standout Performers `Source` column shipped
empty in every saved review that way (issue #267). These tests hold every
prompt table to two rules: the separator row must match the header, and no
column name may go unmentioned in the prompt that declares it.
"""

import importlib
import pkgutil
import re

import pytest

import fpl_cli.prompts

SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")


def _cells(row):
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _prompt_tables():
    """Yield (module, constant, line_no, header_cells, separator_cells, rest).

    Discovery is limited to module-level string constants, which is where the
    prompt templates live. Tables assembled at runtime from FPL data (the
    league recap standings block) are built inside functions and carry no
    instruction text, so they are out of scope.
    """
    tables = []
    for info in pkgutil.iter_modules(fpl_cli.prompts.__path__):
        module = importlib.import_module(f"fpl_cli.prompts.{info.name}")
        for name, value in vars(module).items():
            if name.startswith("_") or not isinstance(value, str):
                continue
            lines = value.split("\n")
            for index, line in enumerate(lines[:-1]):
                if not line.startswith("|") or not SEPARATOR_RE.match(lines[index + 1]):
                    continue
                rest = "\n".join(lines[:index] + lines[index + 2 :])
                tables.append(
                    (
                        info.name,
                        name,
                        index,
                        _cells(line),
                        _cells(lines[index + 1]),
                        rest,
                    )
                )
    return tables


PROMPT_TABLES = _prompt_tables()


def test_prompt_tables_are_discovered():
    """The guard is worthless if discovery silently finds nothing."""
    assert len(PROMPT_TABLES) >= 6
    headers = {" | ".join(table[3]) for table in PROMPT_TABLES}
    assert "Player | Club | Pts | Why They Hauled | Source" in headers


@pytest.mark.parametrize(
    ("module", "constant", "line_no", "header", "separator", "rest"),
    PROMPT_TABLES,
    ids=[f"{t[0]}.{t[1]}:{t[2]}" for t in PROMPT_TABLES],
)
def test_separator_matches_header(module, constant, line_no, header, separator, rest):
    """A separator with the wrong cell count breaks the rendered table."""
    assert len(separator) == len(header), (
        f"{module}.{constant} line {line_no}: header declares {len(header)} columns "
        f"({', '.join(header)}) but the separator row has {len(separator)}"
    )


@pytest.mark.parametrize(
    ("module", "constant", "line_no", "header", "separator", "rest"),
    PROMPT_TABLES,
    ids=[f"{t[0]}.{t[1]}:{t[2]}" for t in PROMPT_TABLES],
)
def test_every_column_is_described(module, constant, line_no, header, separator, rest):
    """Every declared column must be named somewhere else in the prompt.

    Matching is a case-insensitive substring over the rest of the template, so
    the description can live in the bracketed instruction under the table or in
    `<quality_requirements>` - what matters is that the model is told what the
    column is for somewhere, not where the sentence sits.
    """
    lowered = rest.lower()
    undescribed = [column for column in header if column.lower() not in lowered]
    assert not undescribed, (
        f"{module}.{constant} line {line_no}: column(s) {', '.join(undescribed)} "
        "are declared in the table header but never described in the prompt - "
        "the model will leave them blank or improvise"
    )
