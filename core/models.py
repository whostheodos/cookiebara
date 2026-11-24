# Create your models here.
import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import CheckConstraint, Q


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenants"
        ordering = ["-created_at"]
    def __str__(self):
        return f"{self.name} ({self.id})"

class TenantOwnedModel(models.Model):
    """Abstract base class giving every tenant-owned table a UUID PK and tenant FK."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        related_name="%(class)s_set",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

class APIKey(TenantOwnedModel):

    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=16, unique=True)
    key_hash = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)


    class Meta:
        db_table = "api_keys"
        constraints = [
            CheckConstraint(condition=Q(revoked_at__isnull=True) | Q(is_active=False), name="api_key_revoked_implies_inactive"),
        ]


    def __str__(self):
        return f"{self.name} ({self.prefix}...)"

class Endpoint(TenantOwnedModel):
    url = models.URLField(max_length=2048)
    description = models.TextField(blank=True)
    encrypted_secret = models.BinaryField() #fernet cyphertext stored as bytes
    subscribed_events = ArrayField(
        models.CharField(max_length=100),
        default=list,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "endpoints"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="unique_tenant_id",
            ),
            CheckConstraint(
                condition = Q(disabled_at__isnull=True) | Q(is_active=False),
                name="disabled_at_implies_inactive",
            ),
        ]
    def __str__(self):
        return f"{self.url} (tenant {self.tenant.id})"

class Event(TenantOwnedModel):
    idempotency_key = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100, db_index=True)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64, db_index=True)
    payload_bytes = models.BinaryField() #exact raw bytes captured

    class Meta:
        db_table ="events"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                name="unique_tenant_idempotency_key",
            ),
            models.UniqueConstraint(
                fields=["tenant", "id"],
                name="unique_event_tenant_id",
            ),
        ]
    def __str__(self):
        return f"{self.event_type} ({self.id})"

class DeliveryState(models.TextChoices):
    PENDING = "pending", "Pending"
    IN_FLIGHT = "in_flight", "In Flight"
    SUCCEEDED = "succeeded", "Succeeded"
    DEAD = "dead", "Dead"


class DeadReason(models.TextChoices):
    NON_2XX= "non_2xx","non-2xx Response"
    TIMEOUT= "timeout", "Timeout"
    NETWORK_ERROR= "network_error", "Network Error"
    URL_REJECTED= "url_rejected","URL Rejected"
    ENDPOINT_DISABLED= "endpoint_disabled","Endpoint Disabled"
    PERMENANT_4XX= "permanent_4xx","Permanent 4xx Response"
    REDIRECT_NOT_FOLLOWED= "redirect_not_followed","Redirect Not Followed"
    BUDGET_EXHAUSTED= "budget_exhausted","Budget Exhausted"
    CRASH_LOOP= "crash_loop","Crash Loop"

class Delivery(TenantOwnedModel):
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    endpoint = models.ForeignKey(
        Endpoint,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    state = models.CharField(
        max_length=20,
        choices=DeliveryState.choices,
        default=DeliveryState.PENDING,
    )
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    dead_reason = models.CharField(max_length=32,choices=DeadReason.choices, null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "deliveries"
        constraints =[
            models.UniqueConstraint(
                fields=["event", "endpoint"], #one delivery per event-endpoint pair
                name="unique_event_endpoint",
            ),
            CheckConstraint(
                condition=~Q(state=DeliveryState.PENDING) #pending requirements
                | (Q(next_attempt_at__isnull=False) & Q(completed_at__isnull=True)),
                name="delivery_pending_shape",
            ),
            CheckConstraint(
                condition=~Q(state=DeliveryState.DEAD)
                | (Q(dead_reason__isnull=False) & Q(completed_at__isnull=False)),
                name="delivery_dead_shape",
            ),

        ]
        indexes = [
            models.Index(
                fields=["next_attempt_at"],
                condition=Q(state=DeliveryState.PENDING),
                name="idx_delivery_next_attempt_at",
            )
        ]
    def __str__(self):
        return f"Delivery {self.id} []{self.state}] for event {self.event.id} to endpoint {self.endpoint.id}"