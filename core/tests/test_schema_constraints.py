import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from core.models import Delivery, DeliveryState, Endpoint, Event, Tenant


@pytest.fixture
def two_tenants(db):
    tenant1 = Tenant.objects.create(name="Tenant 1")
    tenant2 = Tenant.objects.create(name="Tenant 2")
    return tenant1, tenant2

def make_event(tenant, key="k1"):
    return Event.objects.create(tenant=tenant,
                idempotency_key=key,
                event_type="x.created",
                payload={},
                payload_hash= "0" * 64,
                payload_bytes=b"{}",
            )

def make_endpoint(tenant):
    return Endpoint.objects.create(tenant=tenant, url="https://example.com/hook", encrypted_secret=b"x")

def test_db_rejects_cross_tenant_delivery(two_tenants):
    tenant1, tenant2 = two_tenants
    with pytest.raises(IntegrityError), transaction.atomic():
        Delivery.objects.create(
            tenant=tenant1,
            event=make_event(tenant1, "k1"),
            endpoint=make_endpoint(tenant2),
            next_attempt_at=timezone.now(),
        )

def test_db_rejects_dublicate_delivery(two_tenants):
    tenant1, _ = two_tenants
    event = make_event(tenant1, "k1")
    endpoint = make_endpoint(tenant1)
    Delivery.objects.create(
        tenant=tenant1,
        event=event,
        endpoint=endpoint,
        next_attempt_at=timezone.now(),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Delivery.objects.create(
            tenant=tenant1,
            event=event,
            endpoint=endpoint,
            next_attempt_at=timezone.now(),
        )

def test_db_rejects_pending_without_next_attempt(two_tenants):
    tenant1, _ = two_tenants
    with pytest.raises(IntegrityError), transaction.atomic():
        Delivery.objects.create(
            tenant=tenant1, event=make_event(tenant1), endpoint=make_endpoint(tenant1)
        )

def test_db_rejects_dead_without_reason(two_tenants):
    tenant1, _ = two_tenants
    with pytest.raises(IntegrityError), transaction.atomic():
        Delivery.objects.create(
            tenant=tenant1,
            event=make_event(tenant1),
            endpoint=make_endpoint(tenant1),
            state=DeliveryState.DEAD,
            completed_at=timezone.now(),
        )


def test_event_delete_blocked_by_delivery(two_tenants):
    tenant1, _ = two_tenants
    event = make_event(tenant1)
    Delivery.objects.create(
        tenant=tenant1,
        event=event,
        endpoint=make_endpoint(tenant1),
        next_attempt_at=timezone.now(),
    )
    with pytest.raises(ProtectedError):
        event.delete()