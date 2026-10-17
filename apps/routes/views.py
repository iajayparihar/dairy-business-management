from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.pagination import StandardPagination
from apps.deliveries.services import MANAGE_ROLES
from apps.deliveries.views import DeliveryViewMixin
from apps.routes import services
from apps.routes.models import Route, RouteRun, StandingOrder
from apps.routes.serializers import (
    EntrySerializer,
    ReopenSerializer,
    RouteCreateSerializer,
    RouteCustomersSerializer,
    RouteSerializer,
    RunSerializer,
    RunStartSerializer,
    StandingOrderInputSerializer,
    StandingOrderSerializer,
    VisitSerializer,
)


def query_value(params, name, field):
    """A validated query-string value (400 when malformed), else None."""
    return field.run_validation(params[name]) if name in params else None


class RoutesView(DeliveryViewMixin, APIView):
    def visible_run(self, request, business_id, pk):
        membership = self.get_membership(request, business_id)
        try:
            run = services.visible_runs(membership).get(pk=pk)
        except RouteRun.DoesNotExist as exc:
            raise NotFound("Route run not found.") from exc
        return membership, run

    def tasks_payload(self, run):
        return {
            "run": RunSerializer(run).data,
            "summary": services.run_summary(run),
            "tasks": [
                {
                    **VisitSerializer(t["visit"]).data,
                    "suggestion_source": t["suggestion_source"],
                    "suggestion": t["suggestion"],
                }
                for t in services.run_tasks(run)
            ],
        }


class RouteListCreateView(RoutesView):
    def get(self, request, business_id):
        membership = self.get_membership(request, business_id)
        routes = Route.objects.filter(business_id=business_id, is_active=True)
        if membership.role not in MANAGE_ROLES:
            routes = routes.filter(assigned_to=membership)
        return Response(RouteSerializer(routes, many=True).data)

    def post(self, request, business_id):
        s = RouteCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        route = self.run(
            services.create_route,
            business_id=business_id, user=request.user, **s.validated_data,
        )
        return Response(RouteSerializer(route).data, status=status.HTTP_201_CREATED)


class RouteCustomersView(RoutesView):
    def _route_response(self, route):
        stops = route.stops.select_related("customer").order_by("sequence")
        return Response([
            {"customer": c.customer_id, "name": c.customer.name, "sequence": c.sequence,
             "phone": c.customer.phone, "address": c.customer.address}
            for c in stops
        ])

    def get(self, request, business_id, pk):
        membership = self.get_membership(request, business_id)
        try:
            route = Route.objects.get(pk=pk, business_id=business_id)
        except Route.DoesNotExist as exc:
            raise NotFound("Route not found.") from exc
        if membership.role not in MANAGE_ROLES and route.assigned_to_id != membership.pk:
            raise PermissionDenied("This route is not assigned to you.")
        return self._route_response(route)

    def put(self, request, business_id, pk):
        s = RouteCustomersSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        route = self.run(
            services.set_route_customers,
            business_id=business_id, route_id=pk, user=request.user,
            customer_ids=s.validated_data["customers"],
        )
        return self._route_response(route)


class StandingOrderListCreateView(RoutesView):
    def get(self, request, business_id):
        self.get_membership(request, business_id, roles=MANAGE_ROLES)
        qs = StandingOrder.objects.filter(
            business_id=business_id, is_active=True,
        ).select_related("customer", "product").order_by("customer__name")
        customer = query_value(request.query_params, "customer", serializers.UUIDField())
        if customer:
            qs = qs.filter(customer_id=customer)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(StandingOrderSerializer(page, many=True).data)

    def post(self, request, business_id):
        s = StandingOrderInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        order = self.run(
            services.set_standing_order,
            business_id=business_id, user=request.user, **s.validated_data,
        )
        return Response(StandingOrderSerializer(order).data, status=status.HTTP_201_CREATED)


class StandingOrderDetailView(RoutesView):
    def delete(self, request, business_id, pk):
        self.run(
            services.remove_standing_order,
            business_id=business_id, user=request.user, order_id=pk,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class RunListStartView(RoutesView):
    def get(self, request, business_id):
        membership = self.get_membership(request, business_id)
        runs = services.visible_runs(membership)
        day = query_value(request.query_params, "date", serializers.DateField())
        if day:
            runs = runs.filter(run_date=day)
        state = query_value(
            request.query_params, "status",
            serializers.ChoiceField(choices=RouteRun.Status.values),
        )
        if state:
            runs = runs.filter(status=state)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(runs, request)
        return paginator.get_paginated_response(RunSerializer(page, many=True).data)

    def post(self, request, business_id):
        s = RunStartSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        run = self.run(
            services.start_run,
            business_id=business_id, user=request.user, route_id=d["route_id"],
            run_date=d.get("run_date"), shift=d["shift"],
        )
        return Response(RunSerializer(run).data, status=status.HTTP_201_CREATED)


class RunTasksView(RoutesView):
    def get(self, request, business_id, pk):
        _, run = self.visible_run(request, business_id, pk)
        return Response(self.tasks_payload(run))


class RunEntryView(RoutesView):
    def post(self, request, business_id, pk):
        _, run = self.visible_run(request, business_id, pk)
        s = EntrySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        visit = self.run(
            services.record_entry,
            business_id=business_id, run_id=run.pk, user=request.user,
            customer_id=d["customer_id"], outcome=d["outcome"],
            items=[dict(i) for i in d.get("items", [])],
            reason=d["reason"], client_ref=d.get("client_ref"),
        )
        return Response(VisitSerializer(visit).data)


class RunCloseView(RoutesView):
    def post(self, request, business_id, pk):
        _, run = self.visible_run(request, business_id, pk)
        run = self.run(services.close_run, business_id=business_id, run_id=run.pk, user=request.user)
        return Response({"run": RunSerializer(run).data, "summary": services.run_summary(run)})


class RunReopenView(RoutesView):
    def post(self, request, business_id, pk):
        self.get_membership(request, business_id, roles=MANAGE_ROLES)
        s = ReopenSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        run = self.run(
            services.reopen_run, business_id=business_id, run_id=pk,
            user=request.user, reason=s.validated_data["reason"],
        )
        return Response(RunSerializer(run).data)
