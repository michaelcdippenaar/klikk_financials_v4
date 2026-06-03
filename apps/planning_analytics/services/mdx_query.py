"""
TM1 slice-and-dice query service — generates MDX, executes against the TM1 REST
API (/api/v1/ExecuteMDX), and returns a structured cellset for the console pivot.
PAW is never exposed; the browser talks to Django, Django talks to TM1.
"""
import requests
import urllib3
from requests.auth import HTTPBasicAuth
from .tm1_client import _resolve_credentials

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SYSTEM_PREFIX = "}"


def _session():
    base, user, pw = _resolve_credentials()
    if not base:
        raise ValueError("No active TM1 server configuration.")
    s = requests.Session()
    s.auth = HTTPBasicAuth(user, pw)
    return base.rstrip("/"), s


def list_cubes(include_system=False):
    base, s = _session()
    r = s.get(base + "/Cubes?$select=Name", timeout=30, verify=False)
    r.raise_for_status()
    names = [c["Name"] for c in r.json().get("value", [])]
    if not include_system:
        names = [n for n in names if not n.startswith(SYSTEM_PREFIX)]
    return names


def cube_dimensions(cube):
    base, s = _session()
    r = s.get(base + "/Cubes('%s')?$expand=Dimensions($select=Name)" % cube, timeout=30, verify=False)
    r.raise_for_status()
    return [d["Name"] for d in r.json().get("Dimensions", [])]


def dimension_elements(dim, top=5000):
    base, s = _session()
    r = s.get(base + "/Dimensions('%s')/Hierarchies('%s')/Elements?$select=Name,Type&$top=%d" % (dim, dim, top),
              timeout=60, verify=False)
    r.raise_for_status()
    return [{"name": e["Name"], "type": e.get("Type")} for e in r.json().get("value", [])]


def _member(dim, el):
    el = str(el).replace("]", "]]")
    return "[%s].[%s].[%s]" % (dim, dim, el)


def _axis_set(spec):
    """spec: list of {dimension, members:[...]}. Crossjoin across dims."""
    parts = []
    for d in spec or []:
        dim = d["dimension"]
        members = d.get("members") or []
        if not members:
            continue
        parts.append("{" + ",".join(_member(dim, m) for m in members) + "}")
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = "CROSSJOIN(%s,%s)" % (out, p)
    return out


def build_mdx(cube, rows, cols, filters=None, suppress=True):
    rowset = _axis_set(rows)
    colset = _axis_set(cols)
    if not rowset or not colset:
        raise ValueError("Both rows and cols must specify at least one dimension with members.")
    ne = "NON EMPTY " if suppress else ""
    where = ""
    if filters:
        where = " WHERE (" + ",".join(_member(dim, m) for dim, m in filters.items()) + ")"
    return "SELECT %s%s ON 0, %s%s ON 1 FROM [%s]%s" % (ne, colset, ne, rowset, cube, where)


def execute_mdx(mdx):
    base, s = _session()
    url = (base + "/ExecuteMDX?$expand=Axes($expand=Tuples($expand=Members($select=Name)))"
                  ",Cells($select=Ordinal,Value,FormattedValue)")
    r = s.post(url, json={"MDX": mdx}, timeout=120, verify=False)
    if r.status_code >= 400:
        raise ValueError("TM1 MDX error %d: %s" % (r.status_code, r.text[:400]))
    data = r.json()
    axes = data.get("Axes", [])
    col_tuples = [[m["Name"] for m in t["Members"]] for t in axes[0]["Tuples"]] if len(axes) > 0 else []
    row_tuples = [[m["Name"] for m in t["Members"]] for t in axes[1]["Tuples"]] if len(axes) > 1 else []
    cells = data.get("Cells", [])
    ncols = len(col_tuples) or 1
    grid = []
    for ri, rt in enumerate(row_tuples):
        vals = []
        for ci in range(ncols):
            ordinal = ri * ncols + ci
            cell = cells[ordinal] if ordinal < len(cells) else {}
            vals.append({"value": cell.get("Value"), "formatted": cell.get("FormattedValue")})
        grid.append({"members": rt, "cells": vals})
    return {"columns": col_tuples, "rows": grid, "mdx": mdx}


def run_pivot(cube, rows, cols, filters=None, suppress=True):
    return execute_mdx(build_mdx(cube, rows, cols, filters=filters, suppress=suppress))
