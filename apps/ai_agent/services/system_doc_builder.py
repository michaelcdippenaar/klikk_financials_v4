from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from django.apps import apps
from django.conf import settings
from django.urls import get_resolver

from apps.ai_agent.services.tm1_proxy import tm1_request
from apps.planning_analytics.models import TM1ServerConfig


@dataclass(frozen=True)
class BuildOptions:
    include_django: bool = True
    include_tm1: bool = True
    cube_limit: int = 30
    dim_limit_per_cube: int = 50
    url_limit: int = 200
    model_limit: int = 250


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_int(val: Any, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(val)
    except Exception:
        v = default
    return max(min_v, min(v, max_v))


def _list_urls(limit: int) -> list[dict[str, str]]:
    resolver = get_resolver()
    out: list[dict[str, str]] = []

    def walk(patterns, prefix: str = ''):
        nonlocal out
        for p in patterns:
            if len(out) >= limit:
                return
            try:
                route = str(getattr(p, 'pattern', ''))
            except Exception:
                route = ''
            full = (prefix + route).replace('//', '/')

            # URLResolver has .url_patterns; URLPattern does not.
            child_patterns = getattr(p, 'url_patterns', None)
            if child_patterns is not None:
                walk(child_patterns, prefix=full)
                continue

            name = getattr(p, 'name', '') or ''
            callback = getattr(p, 'callback', None)
            cb_name = ''
            if callback is not None:
                cb_name = getattr(callback, '__qualname__', '') or getattr(callback, '__name__', '') or str(callback)

            out.append({
                'path': full,
                'name': name,
                'view': cb_name,
            })

    walk(resolver.url_patterns, prefix='')
    return out


def _list_models(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in apps.get_models():
        if len(rows) >= limit:
            break
        try:
            fields = [f.name for f in model._meta.get_fields() if getattr(f, 'concrete', False)]
        except Exception:
            fields = []
        rows.append({
            'app_label': model._meta.app_label,
            'model': model.__name__,
            'db_table': model._meta.db_table,
            'fields': fields[:50],
        })
    rows.sort(key=lambda r: (r['app_label'], r['model']))
    return rows


def _tm1_metadata(cube_limit: int, dim_limit_per_cube: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        'configured': False,
        'base_url': '',
        'cubes': [],
        'errors': [],
    }

    try:
        cfg = TM1ServerConfig.get_active()
        result['base_url'] = (cfg.base_url if cfg else '') or ''
    except Exception:
        pass

    cubes_resp = tm1_request(method='GET', path='Cubes?$select=Name&$top=' + str(cube_limit))
    if not cubes_resp.get('success'):
        result['errors'].append({
            'step': 'list_cubes',
            'detail': cubes_resp,
        })
        return result

    result['configured'] = True
    cube_rows = (cubes_resp.get('response_body') or {}).get('value') or []
    cube_names = []
    for item in cube_rows:
        name = (item or {}).get('Name')
        if name:
            cube_names.append(name)

    cubes_out = []
    for cube_name in cube_names:
        dims_resp = tm1_request(
            method='GET',
            path=f"Cubes('{cube_name}')?$expand=Dimensions($select=Name)",
        )
        dims = []
        if dims_resp.get('success'):
            dims = [
                (d or {}).get('Name')
                for d in ((dims_resp.get('response_body') or {}).get('Dimensions') or [])
                if (d or {}).get('Name')
            ]
        else:
            result['errors'].append({
                'step': 'cube_dimensions',
                'cube': cube_name,
                'detail': dims_resp,
            })

        cubes_out.append({
            'name': cube_name,
            'dimensions': dims[:dim_limit_per_cube],
        })

    result['cubes'] = cubes_out
    return result


def build_system_document_markdown(*, title: str, options: BuildOptions) -> tuple[str, dict[str, Any]]:
    """
    Returns (markdown, metadata) where metadata is suitable for SystemDocument.metadata.
    """
    metadata: dict[str, Any] = {
        'generated_at': _now_iso(),
        'options': {
            'include_django': options.include_django,
            'include_tm1': options.include_tm1,
            'cube_limit': options.cube_limit,
            'dim_limit_per_cube': options.dim_limit_per_cube,
            'url_limit': options.url_limit,
            'model_limit': options.model_limit,
        },
    }

    lines: list[str] = []
    lines.append(f'# {title}'.strip())
    lines.append('')
    lines.append(f'_Generated at: {metadata["generated_at"]}_')
    lines.append('')
    lines.append('## Purpose')
    lines.append('Describe what this system does, the main users, and the outcomes it produces.')
    lines.append('')

    if options.include_django:
        lines.append('## Django app overview (auto-generated)')
        lines.append('')
        lines.append('### Runtime settings')
        lines.append('')
        lines.append(f'- **DEBUG**: `{bool(getattr(settings, "DEBUG", False))}`')
        lines.append(f'- **ROOT_URLCONF**: `{getattr(settings, "ROOT_URLCONF", "")}`')
        lines.append(f'- **AUTH_USER_MODEL**: `{getattr(settings, "AUTH_USER_MODEL", "")}`')
        lines.append(f'- **AI agent security disabled**: `{bool(getattr(settings, "AI_AGENT_DISABLE_SECURITY", False))}`')
        lines.append('')

        installed_apps = [a for a in getattr(settings, 'INSTALLED_APPS', []) if isinstance(a, str)]
        installed_apps_filtered = [a for a in installed_apps if a.startswith('apps.') or a.startswith('klikk_')]
        metadata['django'] = {
            'installed_apps_count': len(installed_apps),
            'installed_apps_filtered': installed_apps_filtered[:200],
        }

        lines.append('### Installed apps (filtered)')
        lines.append('')
        for a in installed_apps_filtered[:80]:
            lines.append(f'- `{a}`')
        if len(installed_apps_filtered) > 80:
            lines.append(f'- ... ({len(installed_apps_filtered) - 80} more)')
        lines.append('')

        urls = _list_urls(limit=options.url_limit)
        metadata['django']['urls'] = urls
        lines.append('### URL routes (sample)')
        lines.append('')
        lines.append('| Path | Name | View |')
        lines.append('|---|---|---|')
        for u in urls[:options.url_limit]:
            lines.append(f"| `{u['path']}` | `{u['name']}` | `{u['view']}` |")
        lines.append('')

        models = _list_models(limit=options.model_limit)
        metadata['django']['models'] = models
        lines.append('### Data model (sample)')
        lines.append('')
        lines.append('| App | Model | DB table | Fields (first 50) |')
        lines.append('|---|---|---|---|')
        for m in models:
            fields = ', '.join(m.get('fields') or [])
            lines.append(f"| `{m['app_label']}` | `{m['model']}` | `{m['db_table']}` | `{fields}` |")
        lines.append('')

    if options.include_tm1:
        lines.append('## TM1 metadata (auto-generated)')
        lines.append('')
        try:
            tm1_meta = _tm1_metadata(cube_limit=options.cube_limit, dim_limit_per_cube=options.dim_limit_per_cube)
        except Exception as exc:
            tm1_meta = {'configured': False, 'errors': [{'step': 'exception', 'message': str(exc)}]}

        metadata['tm1'] = {
            'configured': bool(tm1_meta.get('configured')),
            'base_url': tm1_meta.get('base_url', ''),
            'cubes': tm1_meta.get('cubes', []),
            'errors_count': len(tm1_meta.get('errors', [])),
        }

        if not tm1_meta.get('configured'):
            lines.append('- TM1 is not configured or could not be reached from this Django instance.')
            lines.append('')
        else:
            lines.append(f'- **Base URL**: `{tm1_meta.get("base_url", "")}`')
            lines.append(f'- **Cube count (sampled)**: `{len(tm1_meta.get("cubes", []))}`')
            lines.append('')

            for cube in tm1_meta.get('cubes', []):
                lines.append(f'### Cube: `{cube.get("name")}`')
                lines.append('')
                lines.append('**Purpose (fill in):**')
                lines.append('- _What business question does this cube answer?_')
                lines.append('- _Who owns it?_')
                lines.append('- _Refresh cadence / source of truth?_')
                lines.append('')
                lines.append('**Dimensions (auto):**')
                for d in cube.get('dimensions') or []:
                    lines.append(f'- `{d}`')
                if not (cube.get('dimensions') or []):
                    lines.append('- (Could not fetch dimensions)')
                lines.append('')

            if tm1_meta.get('errors'):
                lines.append('### TM1 fetch errors (for troubleshooting)')
                lines.append('')
                for e in tm1_meta.get('errors')[:20]:
                    lines.append(f'- `{e.get("step")}`: {str(e.get("cube") or "")} `{str(e.get("detail") or e.get("message") or "")[:500]}`')
                lines.append('')

    lines.append('## Glossary (fill in)')
    lines.append('')
    lines.append('- **Tenant**:')
    lines.append('- **Financial year / period**:')
    lines.append('- **Tracking category 1 vs 2**:')
    lines.append('- **Actuals / Forecast / Sandbox versions**:')
    lines.append('')

    return '\n'.join(lines).strip() + '\n', metadata

