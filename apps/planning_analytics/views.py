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
