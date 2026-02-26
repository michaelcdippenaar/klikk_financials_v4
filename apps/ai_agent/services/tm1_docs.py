from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from apps.ai_agent.services.tm1_proxy import tm1_request
from apps.planning_analytics.models import TM1ServerConfig


@dataclass(frozen=True)
class TM1DocsOptions:
    top_cubes: int = 200
    top_dimensions: int = 200
    top_processes: int = 200
    elements_per_hierarchy: int = 50
    include_elements: bool = False
    include_process_code: bool = False
    include_cube_rules: bool = False
    max_chars_full: int = 400_000


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _odata_quote(name: str) -> str:
    # OData single-quoted string escaping: ' -> ''
    return (name or '').replace("'", "''")


def _extract_value_list(resp: dict[str, Any]) -> list[dict[str, Any]]:
    body = resp.get('response_body') or {}
    val = body.get('value')
    return val if isinstance(val, list) else []


def _name_list(value_rows: list[dict[str, Any]]) -> list[str]:
    out = []
    for r in value_rows:
        n = (r or {}).get('Name')
        if n:
            out.append(str(n))
    return out


def fetch_tm1_names(path: str) -> tuple[list[str], dict[str, Any] | None]:
    resp = tm1_request(method='GET', path=path)
    if not resp.get('success'):
        return [], resp
    return _name_list(_extract_value_list(resp)), None


def _fetch_processes_with_code(top: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Fetch process metadata including TI procedures, but deliberately exclude DataSource/Variables
    (those can contain credentials or noisy details).
    """
    resp = tm1_request(
        method='GET',
        path=(
            "Processes"
            f"?$top={int(top)}"
            "&$select=Name,PrologProcedure,MetadataProcedure,DataProcedure,EpilogProcedure,Parameters"
        ),
    )
    if not resp.get('success'):
        return [], resp
    return _extract_value_list(resp), None


def _fetch_cube_dimensions(cube_name: str) -> tuple[list[str], dict[str, Any] | None]:
    q = _odata_quote(cube_name)
    resp = tm1_request(method='GET', path=f"Cubes('{q}')?$expand=Dimensions($select=Name)")
    if not resp.get('success'):
        return [], resp
    dims = (resp.get('response_body') or {}).get('Dimensions') or []
    out = []
    for d in dims:
        n = (d or {}).get('Name')
        if n:
            out.append(str(n))
    return out, None


def _fetch_dimension_hierarchies(dim_name: str) -> tuple[list[str], dict[str, Any] | None]:
    q = _odata_quote(dim_name)
    resp = tm1_request(method='GET', path=f"Dimensions('{q}')?$expand=Hierarchies($select=Name)")
    if not resp.get('success'):
        return [], resp
    hs = (resp.get('response_body') or {}).get('Hierarchies') or []
    out = []
    for h in hs:
        n = (h or {}).get('Name')
        if n:
            out.append(str(n))
    return out, None


def _fetch_hierarchy_elements(dim_name: str, hier_name: str, top: int) -> tuple[list[str], dict[str, Any] | None]:
    dq = _odata_quote(dim_name)
    hq = _odata_quote(hier_name)
    resp = tm1_request(
        method='GET',
        path=f"Dimensions('{dq}')/Hierarchies('{hq}')?$expand=Elements($select=Name;$top={int(top)})",
    )
    if not resp.get('success'):
        return [], resp
    els = (resp.get('response_body') or {}).get('Elements') or []
    out = []
    for e in els:
        n = (e or {}).get('Name')
        if n:
            out.append(str(n))
    return out, None


def _fetch_cube_rules(cube_name: str) -> tuple[str, dict[str, Any] | None]:
    q = _odata_quote(cube_name)
    resp = tm1_request(method='GET', path=f"Cubes('{q}')/Rules")
    if not resp.get('success'):
        return '', resp
    body = resp.get('response_body') or {}
    txt = body.get('value')
    return (str(txt) if isinstance(txt, str) else ''), None


def _trim(s: str, limit: int) -> str:
    if not s:
        return ''
    if len(s) <= limit:
        return s
    return s[:limit] + "\n... (truncated)\n"


def _process_doc_markdown(p: dict[str, Any]) -> str:
    name = str((p or {}).get('Name') or '').strip()
    params = (p or {}).get('Parameters') or []
    prolog = str((p or {}).get('PrologProcedure') or '')
    metadata = str((p or {}).get('MetadataProcedure') or '')
    data = str((p or {}).get('DataProcedure') or '')
    epilog = str((p or {}).get('EpilogProcedure') or '')

    out: list[str] = []
    out.append(f"# TM1 Process: `{name}`")
    out.append('')
    if params and isinstance(params, list):
        out.append('## Parameters')
        out.append('')
        for prm in params:
            if not isinstance(prm, dict):
                continue
            pname = prm.get('Name')
            ptype = prm.get('Type')
            pval = prm.get('Value')
            if pname:
                out.append(f"- `{pname}` ({ptype or 'unknown'}), default `{pval}`")
        out.append('')

    def _section(title: str, text: str):
        text = (text or '').strip()
        out.append(f"## {title}")
        out.append('')
        if not text:
            out.append('_empty_')
            out.append('')
            return
        out.append('```')
        out.append(text)
        out.append('```')
        out.append('')

    _section('Prolog', prolog)
    _section('Metadata', metadata)
    _section('Data', data)
    _section('Epilog', epilog)
    return '\n'.join(out).strip() + '\n'


def _cube_doc_markdown(*, cube_name: str, cube_dims: list[str], rules_text: str | None) -> str:
    out: list[str] = []
    out.append(f"# TM1 Cube: `{cube_name}`")
    out.append('')
    out.append('## Dimensions')
    out.append('')
    if cube_dims:
        for d in cube_dims:
            out.append(f'- `{d}`')
    else:
        out.append('- (could not fetch dimensions)')
    out.append('')
    if rules_text is not None:
        out.append('## Rules')
        out.append('')
        rules_text = (rules_text or '').strip()
        if not rules_text:
            out.append('_no rules returned_')
            out.append('')
        else:
            out.append('```')
            out.append(rules_text)
            out.append('```')
            out.append('')
    return '\n'.join(out).strip() + '\n'


def _dimension_doc_markdown(
    *,
    dim_name: str,
    hierarchies: list[str],
    elements_by_hierarchy: dict[str, list[str]] | None,
    elements_per_hierarchy: int,
) -> str:
    out: list[str] = []
    out.append(f"# TM1 Dimension: `{dim_name}`")
    out.append('')
    out.append('## Hierarchies')
    out.append('')
    if hierarchies:
        for h in hierarchies:
            out.append(f'- `{h}`')
    else:
        out.append('- (could not fetch hierarchies)')
    out.append('')

    if elements_by_hierarchy:
        out.append('## Elements (sample)')
        out.append('')
        for h in hierarchies:
            els = elements_by_hierarchy.get(h) or []
            out.append(f"### `{h}` (top {elements_per_hierarchy})")
            out.append('')
            if not els:
                out.append('_none returned_')
                out.append('')
                continue
            for e in els:
                out.append(f'- `{e}`')
            out.append('')

    return '\n'.join(out).strip() + '\n'


def build_tm1_docs_bundle(*, options: TM1DocsOptions) -> tuple[dict[str, Any], str, str, dict[str, str]]:
    """
    Build:
      - summary markdown
      - full markdown (may be truncated by max_chars_full)
      - split docs: mapping of doc_key -> markdown, where doc_key is one of:
          cube:<CubeName>, dim:<DimName>, proc:<ProcessName>
    """
    cfg = TM1ServerConfig.get_active()
    base_url = (cfg.base_url if cfg else '') or ''

    meta: dict[str, Any] = {
        'generated_at': _now_iso(),
        'base_url': base_url,
        'options': {
            'top_cubes': options.top_cubes,
            'top_dimensions': options.top_dimensions,
            'top_processes': options.top_processes,
            'elements_per_hierarchy': options.elements_per_hierarchy,
            'include_elements': options.include_elements,
            'include_process_code': options.include_process_code,
            'include_cube_rules': options.include_cube_rules,
        },
        'errors': [],
    }

    cubes, err = fetch_tm1_names(f'Cubes?$select=Name&$top={int(options.top_cubes)}')
    if err:
        meta['errors'].append({'step': 'cubes', 'detail': err})

    dims, err = fetch_tm1_names(f'Dimensions?$select=Name&$top={int(options.top_dimensions)}')
    if err:
        meta['errors'].append({'step': 'dimensions', 'detail': err})

    proc_names, err = fetch_tm1_names(f'Processes?$select=Name&$top={int(options.top_processes)}')
    if err:
        meta['errors'].append({'step': 'processes', 'detail': err})

    proc_rows: list[dict[str, Any]] = []
    proc_by_name: dict[str, dict[str, Any]] = {}
    if options.include_process_code:
        proc_rows, perr = _fetch_processes_with_code(options.top_processes)
        if perr:
            meta['errors'].append({'step': 'processes_with_code', 'detail': perr})
        for r in proc_rows:
            if isinstance(r, dict) and r.get('Name'):
                proc_by_name[str(r.get('Name'))] = r

    # Summary doc: lightweight and designed to be pinned into context.
    s: list[str] = []
    s.append('# TM1 / PAW documentation (summary)')
    s.append('')
    s.append(f'- **Generated at**: `{meta["generated_at"]}`')
    s.append(f'- **Base URL**: `{base_url}`')
    s.append('')
    s.append('## Key assets')
    s.append('')
    s.append(f'- **Cubes** (sampled): `{len(cubes)}`')
    for n in cubes[:50]:
        s.append(f'  - `{n}`')
    if len(cubes) > 50:
        s.append(f'  - ... ({len(cubes) - 50} more)')
    s.append('')
    s.append(f'- **Dimensions** (sampled): `{len(dims)}`')
    for n in dims[:80]:
        s.append(f'  - `{n}`')
    if len(dims) > 80:
        s.append(f'  - ... ({len(dims) - 80} more)')
    s.append('')
    s.append(f'- **Processes** (sampled): `{len(proc_names)}`')
    for n in proc_names[:50]:
        s.append(f'  - `{n}`')
    if len(proc_names) > 50:
        s.append(f'  - ... ({len(proc_names) - 50} more)')
    s.append('')
    s.append('## How to query (examples)')
    s.append('')
    s.append('- List cubes: `paw get Cubes?$select=Name&$top=200`')
    s.append("- Cube dims: `paw get Cubes('Trail_Balance')?$expand=Dimensions($select=Name)`")
    s.append('- MDX: `paw mdx SELECT ...` (uses `ExecuteMDX` under the hood)')
    s.append('')

    summary_md = '\n'.join(s).strip() + '\n'

    split_docs: dict[str, str] = {}

    # Full doc (for convenience) + split cube docs.
    f: list[str] = []
    f.append('# TM1 / PAW documentation (full)')
    f.append('')
    f.append(f'- **Generated at**: `{meta["generated_at"]}`')
    f.append(f'- **Base URL**: `{base_url}`')
    f.append('')

    f.append('## Cubes')
    f.append('')
    for cube in cubes:
        cdims, cerr = _fetch_cube_dimensions(cube)
        if cerr:
            meta['errors'].append({'step': 'cube_dimensions', 'cube': cube, 'detail': cerr})
        rules_text: str | None = None
        if options.include_cube_rules:
            rules_text, rerr = _fetch_cube_rules(cube)
            if rerr:
                meta['errors'].append({'step': 'cube_rules', 'cube': cube, 'detail': rerr})
                rules_text = ''

        split_docs[f'cube:{cube}'] = _cube_doc_markdown(cube_name=cube, cube_dims=cdims, rules_text=rules_text)

        f.append(f"### `{cube}`")
        f.append('')
        if cdims:
            for d in cdims:
                f.append(f'- `{d}`')
        else:
            f.append('- (could not fetch dimensions)')
        if options.include_cube_rules:
            f.append('')
            f.append('#### Rules')
            f.append('')
            f.append('```')
            f.append(_trim((rules_text or '').strip(), 8000).strip())
            f.append('```')
        f.append('')

    # Dimensions + split dimension docs.
    f.append('## Dimensions')
    f.append('')
    for dim in dims:
        hierarchies, derr = _fetch_dimension_hierarchies(dim)
        if derr:
            meta['errors'].append({'step': 'dimension_hierarchies', 'dimension': dim, 'detail': derr})
        elements_by_hierarchy: dict[str, list[str]] | None = None
        if options.include_elements and hierarchies:
            elements_by_hierarchy = {}
            for h in hierarchies:
                els, eerr = _fetch_hierarchy_elements(dim, h, top=options.elements_per_hierarchy)
                if eerr:
                    meta['errors'].append({'step': 'hierarchy_elements', 'dimension': dim, 'hierarchy': h, 'detail': eerr})
                    continue
                elements_by_hierarchy[h] = els

        split_docs[f'dim:{dim}'] = _dimension_doc_markdown(
            dim_name=dim,
            hierarchies=hierarchies,
            elements_by_hierarchy=elements_by_hierarchy,
            elements_per_hierarchy=options.elements_per_hierarchy,
        )

        f.append(f"### `{dim}`")
        f.append('')
        if hierarchies:
            for h in hierarchies:
                f.append(f'- `{h}`')
        else:
            f.append('- (could not fetch hierarchies)')
        f.append('')

    # Processes + split process docs.
    f.append('## Processes')
    f.append('')
    if options.include_process_code:
        # Keep the full doc readable: list names here, rely on split docs for full code.
        for p in proc_names:
            f.append(f'- `{p}`')
    else:
        for p in proc_names:
            f.append(f'- `{p}`')
    f.append('')

    if options.include_process_code:
        for pname in proc_names:
            row = proc_by_name.get(pname) or {'Name': pname}
            split_docs[f'proc:{pname}'] = _process_doc_markdown(row)
    else:
        for pname in proc_names:
            split_docs[f'proc:{pname}'] = f"# TM1 Process: `{pname}`\n\n_Imported without TI code (include_process_code=false)_\n"

    if meta['errors']:
        f.append('## Errors / gaps')
        f.append('')
        f.append('Some metadata calls failed; details captured in document metadata.')
        f.append('')

    full_md = '\n'.join(f).strip() + '\n'
    if len(full_md) > options.max_chars_full:
        full_md = full_md[: options.max_chars_full] + '\n... (truncated)\n'

    return meta, summary_md, full_md, split_docs


def build_tm1_docs(*, options: TM1DocsOptions) -> tuple[dict[str, Any], str, str]:
    """
    Returns (metadata, summary_markdown, full_markdown).
    """
    meta, summary_md, full_md, _split = build_tm1_docs_bundle(options=options)
    return meta, summary_md, full_md

