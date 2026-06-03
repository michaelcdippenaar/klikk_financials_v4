from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.planning_analytics.models import TM1ServerConfig, TM1ProcessConfig, UserTM1Credentials
from apps.planning_analytics.services.tm1_client import execute_process, test_connection


def _get_user_tm1_creds(request):
    """Return (tm1_username, tm1_password) for the authenticated user, or (None, None)."""
    user = getattr(request, 'user', None)
    if user and getattr(user, 'is_authenticated', False):
        try:
            creds = user.tm1_credentials
            return creds.tm1_username, creds.tm1_password
        except UserTM1Credentials.DoesNotExist:
            pass
    return None, None


class UserTM1CredentialsView(APIView):
    """GET / PUT per-user TM1 credentials."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            creds = request.user.tm1_credentials
            return Response({
                'tm1_username': creds.tm1_username,
                'tm1_password': '********' if creds.tm1_password else '',
            })
        except UserTM1Credentials.DoesNotExist:
            return Response({'tm1_username': '', 'tm1_password': ''})

    def put(self, request):
        tm1_username = (request.data.get('tm1_username', '') or '').strip()
        tm1_password = request.data.get('tm1_password', '')

        if not tm1_username:
            return Response({'error': 'tm1_username is required'}, status=status.HTTP_400_BAD_REQUEST)

        creds, created = UserTM1Credentials.objects.update_or_create(
            user=request.user,
            defaults={
                'tm1_username': tm1_username,
                **(
                    {'tm1_password': tm1_password}
                    if tm1_password and tm1_password != '********'
                    else {}
                ),
            },
        )
        return Response({
            'tm1_username': creds.tm1_username,
            'message': 'TM1 credentials saved.',
        })

    def delete(self, request):
        deleted, _ = UserTM1Credentials.objects.filter(user=request.user).delete()
        if deleted:
            return Response({'message': 'TM1 credentials removed.'})
        return Response({'message': 'No TM1 credentials to remove.'})


class PipelineRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.planning_analytics.services.pipeline import run_pipeline

        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        user_tm1, user_pw = _get_user_tm1_creds(request)

        result = run_pipeline(
            tenant_id,
            load_all=request.data.get('load_all', False),
            rebuild_trail_balance=request.data.get('rebuild_trail_balance', False),
            exclude_manual_journals=request.data.get('exclude_manual_journals', False),
            calculate_pnl_ytd=request.data.get('calculate_pnl_ytd', True),
            tm1_processes=request.data.get('tm1_processes'),
            tm1_user=user_tm1,
            tm1_password=user_pw,
        )
        return Response(result)


class TM1ExecuteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        process_name = request.data.get('process_name')
        if not process_name:
            return Response({'error': 'process_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        user_tm1, user_pw = _get_user_tm1_creds(request)
        parameters = request.data.get('parameters')
        result = execute_process(process_name, parameters=parameters, user=user_tm1, password=user_pw)
        http_status = status.HTTP_200_OK if result['success'] else status.HTTP_502_BAD_GATEWAY
        return Response(result, status=http_status)


class TM1TestConnectionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_tm1, user_pw = _get_user_tm1_creds(request)
        result = test_connection(
            base_url=request.data.get('base_url'),
            user=request.data.get('user') or user_tm1,
            password=request.data.get('password') or user_pw,
        )
        http_status = status.HTTP_200_OK if result['success'] else status.HTTP_502_BAD_GATEWAY
        return Response(result, status=http_status)


class TM1ConfigView(APIView):
    """GET / POST the active TM1 server configuration."""
    permission_classes = [AllowAny]

    def get(self, request):
        cfg = TM1ServerConfig.get_active()
        if not cfg:
            return Response({'base_url': '', 'username': '', 'password': ''})
        return Response({
            'id': cfg.id,
            'base_url': cfg.base_url,
            'username': cfg.username,
            'password': '********' if cfg.password else '',
        })

    def post(self, request):
        base_url = (request.data.get('base_url', '') or '').strip()
        username = (request.data.get('username', '') or '').strip()
        password = request.data.get('password', '')

        cfg = TM1ServerConfig.get_active()
        if cfg:
            cfg.base_url = base_url
            cfg.username = username
            if password and password != '********':
                cfg.password = password
            cfg.save()
        else:
            cfg = TM1ServerConfig.objects.create(
                base_url=base_url,
                username=username,
                password=password,
                is_active=True,
            )

        return Response({
            'id': cfg.id,
            'base_url': cfg.base_url,
            'username': cfg.username,
            'message': 'TM1 server config saved.',
        })


class TM1ProcessListView(APIView):
    """GET / POST the list of TM1 TI processes."""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = TM1ProcessConfig.objects.all()
        data = [
            {
                'id': p.id,
                'process_name': p.process_name,
                'enabled': p.enabled,
                'sort_order': p.sort_order,
                'parameters': p.parameters,
            }
            for p in qs
        ]
        return Response(data)

    def post(self, request):
        """Replace all process configs with the submitted list."""
        processes = request.data if isinstance(request.data, list) else request.data.get('processes', [])

        TM1ProcessConfig.objects.all().delete()
        created = []
        for idx, p in enumerate(processes):
            obj = TM1ProcessConfig.objects.create(
                process_name=p.get('process_name', ''),
                enabled=p.get('enabled', True),
                sort_order=p.get('sort_order', idx),
                parameters=p.get('parameters', {}),
            )
            created.append({
                'id': obj.id,
                'process_name': obj.process_name,
                'enabled': obj.enabled,
                'sort_order': obj.sort_order,
                'parameters': obj.parameters,
            })
        return Response({'message': f'{len(created)} process(es) saved.', 'processes': created})


class TrackingMappingView(APIView):
    """
    GET  ?tenant_id=<id>  — compare Xero tracking_category_1 options vs TM1 dimensions.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant_id = request.query_params.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Fetch Xero tracking category 1 options for this tenant
        try:
            from apps.xero.xero_metadata.models import XeroTracking
            from apps.xero.xero_core.models import XeroTenant
            try:
                tenant = XeroTenant.objects.get(xero_tenant_id=tenant_id)
            except XeroTenant.DoesNotExist:
                return Response({'error': 'Tenant not found'}, status=status.HTTP_404_NOT_FOUND)

            # Try matching by tracking_category_1_id (stable Xero ID) first
            cat1_id = getattr(tenant, 'tracking_category_1_id', None)
            if cat1_id:
                qs = XeroTracking.objects.filter(
                    organisation=tenant,
                    tracking_category_id=cat1_id,
                )
            else:
                # Fall back to category_slot
                qs = XeroTracking.objects.filter(organisation=tenant, category_slot=1)

            xero_options = sorted([t.option for t in qs if t.option])
        except Exception as exc:
            return Response({'error': f'Failed to load Xero tracking options: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Fetch TM1 dimension elements
        user_tm1, user_pw = _get_user_tm1_creds(request)
        from apps.planning_analytics.services.tm1_client import get_dimension_elements

        t1_result = get_dimension_elements('tracking_1', user=user_tm1, password=user_pw)
        co_result = get_dimension_elements('cost_object', user=user_tm1, password=user_pw)

        if not t1_result['success']:
            return Response({'error': f"TM1 tracking_1 error: {t1_result['message']}"}, status=status.HTTP_502_BAD_GATEWAY)
        if not co_result['success']:
            return Response({'error': f"TM1 cost_object error: {co_result['message']}"}, status=status.HTTP_502_BAD_GATEWAY)

        tm1_t1_lower = {e.lower() for e in t1_result['elements']}
        tm1_co_lower = {e.lower() for e in co_result['elements']}

        rows = [
            {
                'xero_name': opt,
                'in_tracking1': opt.lower() in tm1_t1_lower,
                'in_cost_object': opt.lower() in tm1_co_lower,
            }
            for opt in xero_options
        ]

        return Response({
            'xero_options': xero_options,
            'tm1_tracking1': t1_result['elements'],
            'tm1_cost_object': co_result['elements'],
            'rows': rows,
            'unmapped_count': sum(1 for r in rows if not r['in_tracking1']),
        })


class TrackingMappingAddView(APIView):
    """
    POST — add a Xero tracking element to TM1 tracking_1 and/or cost_object dimensions.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        xero_name = (request.data.get('xero_name') or '').strip()
        if not xero_name:
            return Response({'error': 'xero_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        add_to_tracking1 = request.data.get('add_to_tracking1', True)
        add_to_cost_object = request.data.get('add_to_cost_object', False)
        cost_object_name = (request.data.get('cost_object_name') or '').strip() or xero_name

        user_tm1, user_pw = _get_user_tm1_creds(request)
        from apps.planning_analytics.services.tm1_client import create_dimension_element

        actions = []

        if add_to_tracking1:
            result = create_dimension_element(
                'tracking_1', xero_name,
                parent_name='All_Tracking_1',
                user=user_tm1, password=user_pw,
            )
            actions.append({'dimension': 'tracking_1', 'element': xero_name, **result})

        if add_to_cost_object:
            result = create_dimension_element(
                'cost_object', cost_object_name,
                parent_name='All_Cost_Object',
                user=user_tm1, password=user_pw,
            )
            actions.append({'dimension': 'cost_object', 'element': cost_object_name, **result})

        overall_success = all(a.get('success') for a in actions)
        return Response({'xero_name': xero_name, 'actions': actions, 'success': overall_success})


# ============================================================================
# TM1 slice-and-dice (PAW-free): metadata + MDX pivot query
# ============================================================================
from apps.planning_analytics.services import mdx_query as _mdx  # noqa: E402


class TM1CubesView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        try:
            return Response({"cubes": _mdx.list_cubes()})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class TM1CubeDimensionsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        cube = request.query_params.get("cube")
        if not cube:
            return Response({"error": "cube is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response({"cube": cube, "dimensions": _mdx.cube_dimensions(cube)})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class TM1DimensionElementsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        dim = request.query_params.get("dimension")
        if not dim:
            return Response({"error": "dimension is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response({"dimension": dim, "elements": _mdx.dimension_elements(dim)})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class TM1DimensionChildrenView(APIView):
    """GET ?dimension=X&parent=Y -> direct children (components) of a consolidated
    element, so the pivot can default to / drill into a rollup's children rather
    than the whole flat element list."""
    permission_classes = [IsAuthenticated]
    def get(self, request):
        dim = request.query_params.get("dimension")
        parent = request.query_params.get("parent")
        if not dim or not parent:
            return Response({"error": "dimension and parent are required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response({"children": _mdx.dimension_children(dim, parent)})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class TM1PivotQueryView(APIView):
    """POST { cube, rows:[{dimension,members[]}], cols:[...], filters:{dim:member}, suppress }"""
    permission_classes = [IsAuthenticated]
    def post(self, request):
        d = request.data
        cube = d.get("cube")
        rows = d.get("rows") or []
        cols = d.get("cols") or []
        if not cube or not rows or not cols:
            return Response({"error": "cube, rows and cols are required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = _mdx.run_pivot(cube, rows, cols, filters=d.get("filters") or {},
                                    suppress=bool(d.get("suppress", True)))
            return Response(result)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


from apps.planning_analytics.services import cost_analysis as _costcut  # noqa: E402


class CostCutReportView(APIView):
    """GET /api/planning-analytics/cost-cut/?entity=<uuid>&year=<YYYY>[&groups=A,B]
    Recurring-Cash Cost-Cut Finder — leaf expense accounts, this year vs prior,
    ranked by size and YoY growth. Reads live from TM1 (gl_src_trial_balance)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        entity = request.query_params.get("entity")
        year = request.query_params.get("year")
        if not entity or not year:
            return Response({"error": "entity and year are required"}, status=status.HTTP_400_BAD_REQUEST)
        groups_q = request.query_params.get("groups")
        groups = [g.strip() for g in groups_q.split(",") if g.strip()] if groups_q else None
        try:
            return Response(_costcut.cost_cut_report(entity, year, groups=groups))
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


from apps.planning_analytics.models import KPITarget  # noqa: E402
from django.db import IntegrityError  # noqa: E402


def _serialize_target(t):
    return {
        "id": t.id,
        "metric_key": t.metric_key,
        "entity_id": t.entity_id,
        "period_year": t.period_year,
        "label": t.label,
        "target_value": float(t.target_value),
        "direction": t.direction,
        "amber_band_pct": float(t.amber_band_pct),
        "note": t.note,
        "updated_at": t.updated_at.isoformat(),
    }


class KPITargetView(APIView):
    """Editable performance targets driving the cockpit RAG flags.

    GET    ?entity=<uuid|''>&year=<YYYY>[&metric_key=]  -> list targets
    POST   {metric_key, period_year, target_value, entity_id?, label?,
            direction?, amber_band_pct?, note?}          -> upsert (by metric_key+entity+year)
    DELETE ?id=<id>   OR  ?metric_key=&entity=&year=     -> remove a target
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        year = request.query_params.get("year")
        entity = request.query_params.get("entity")
        qs = KPITarget.objects.all()
        if year:
            qs = qs.filter(period_year=year)
        if entity is not None:
            qs = qs.filter(entity_id__in=[entity, ""])
        mk = request.query_params.get("metric_key")
        if mk:
            qs = qs.filter(metric_key=mk)
        return Response({"targets": [_serialize_target(t) for t in qs]})

    def post(self, request):
        d = request.data
        mk = (d.get("metric_key") or "").strip()
        year = d.get("period_year", d.get("year"))
        val = d.get("target_value")
        if not mk or year in (None, "") or val in (None, ""):
            return Response({"error": "metric_key, period_year and target_value are required"},
                            status=status.HTTP_400_BAD_REQUEST)
        defaults = {
            "label": d.get("label", ""),
            "target_value": val,
            "direction": d.get("direction", KPITarget.LOWER),
            "amber_band_pct": d.get("amber_band_pct", 5),
            "note": d.get("note", ""),
            "created_by": request.user if request.user.is_authenticated else None,
        }
        try:
            obj, created = KPITarget.objects.update_or_create(
                metric_key=mk, entity_id=(d.get("entity_id") or d.get("entity") or ""), period_year=int(year),
                defaults=defaults,
            )
        except (ValueError, IntegrityError) as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_serialize_target(obj),
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request):
        tid = request.query_params.get("id")
        if tid:
            KPITarget.objects.filter(id=tid).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        mk = request.query_params.get("metric_key")
        year = request.query_params.get("year")
        entity = request.query_params.get("entity") or ""
        if mk and year:
            KPITarget.objects.filter(metric_key=mk, entity_id=entity, period_year=year).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response({"error": "provide id, or metric_key+year(+entity)"},
                        status=status.HTTP_400_BAD_REQUEST)


from apps.planning_analytics.models import CostBehaviour  # noqa: E402


def _serialize_behaviour(cb):
    return {
        "id": cb.id, "account_key": cb.account_key, "behaviour": cb.behaviour,
        "driver": cb.driver, "source": cb.source, "note": cb.note,
        "cuttability": cb.cuttability, "is_addressable": cb.is_addressable,
        "is_manageable": cb.is_manageable,
        "updated_at": cb.updated_at.isoformat(),
    }


class CostBehaviourView(APIView):
    """Cost-behaviour classification (CFO seed + user overrides).

    GET  [?behaviour=fixed]                 -> list classifications
    POST {account_key, behaviour[, driver, note]} -> re-tag (user_override); upsert by account_key
    """
    permission_classes = [IsAuthenticated]
    _VALID = {c[0] for c in CostBehaviour.BEHAVIOUR_CHOICES}

    def get(self, request):
        qs = CostBehaviour.objects.all()
        beh = request.query_params.get("behaviour")
        if beh:
            qs = qs.filter(behaviour=beh)
        return Response({"classifications": [_serialize_behaviour(c) for c in qs]})

    _VALID_TIERS = {c[0] for c in CostBehaviour.TIERS}

    def post(self, request):
        d = request.data
        key = (d.get("account_key") or "").strip()
        beh = (d.get("behaviour") or "").strip()
        tier = (d.get("cuttability") or "").strip()
        if not key:
            return Response({"error": "account_key is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not beh and not tier and "is_manageable" not in d:
            return Response({"error": "provide behaviour, cuttability and/or is_manageable"}, status=status.HTTP_400_BAD_REQUEST)
        if beh and beh not in self._VALID:
            return Response({"error": "invalid behaviour (%s)" % ", ".join(sorted(self._VALID))},
                            status=status.HTTP_400_BAD_REQUEST)
        if tier and tier not in self._VALID_TIERS:
            return Response({"error": "invalid cuttability (%s)" % ", ".join(sorted(self._VALID_TIERS))},
                            status=status.HTTP_400_BAD_REQUEST)
        defaults = {
            "source": CostBehaviour.OVERRIDE,
            "updated_by": request.user if request.user.is_authenticated else None,
        }
        if beh:
            defaults["behaviour"] = beh
        if tier:
            defaults["cuttability"] = tier
            # T0 is the below-the-line tier; keep is_addressable consistent with it.
            defaults["is_addressable"] = (tier != "T0")
        if "driver" in d:
            defaults["driver"] = d.get("driver") or ""
        if "note" in d:
            defaults["note"] = d.get("note") or ""
        if "is_manageable" in d:
            defaults["is_manageable"] = bool(d.get("is_manageable"))
        # update_or_create needs a behaviour for brand-new rows; fall back to a
        # neutral default if only a tier was supplied for an unseen account.
        obj, created = CostBehaviour.objects.update_or_create(
            account_key=key,
            defaults=defaults,
            create_defaults={**defaults, "behaviour": defaults.get("behaviour", CostBehaviour.VARIABLE)},
        )
        return Response(_serialize_behaviour(obj),
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
