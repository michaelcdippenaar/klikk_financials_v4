from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from apps.planning_analytics.models import TM1ServerConfig, TM1ProcessConfig
from apps.planning_analytics.services.tm1_client import execute_process, test_connection
from apps.planning_analytics.services.pipeline import run_pipeline


class PipelineRunView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        tenant_id = request.data.get('tenant_id')
        if not tenant_id:
            return Response({'error': 'tenant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        result = run_pipeline(
            tenant_id,
            load_all=request.data.get('load_all', False),
            rebuild_trail_balance=request.data.get('rebuild_trail_balance', False),
            exclude_manual_journals=request.data.get('exclude_manual_journals', False),
            calculate_pnl_ytd=request.data.get('calculate_pnl_ytd', True),
            tm1_processes=request.data.get('tm1_processes'),
        )
        return Response(result)


class TM1ExecuteView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        process_name = request.data.get('process_name')
        if not process_name:
            return Response({'error': 'process_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        parameters = request.data.get('parameters')
        result = execute_process(process_name, parameters=parameters)
        http_status = status.HTTP_200_OK if result['success'] else status.HTTP_502_BAD_GATEWAY
        return Response(result, status=http_status)


class TM1TestConnectionView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        result = test_connection(
            base_url=request.data.get('base_url'),
            user=request.data.get('user'),
            password=request.data.get('password'),
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
        base_url = request.data.get('base_url', '')
        username = request.data.get('username', '')
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
