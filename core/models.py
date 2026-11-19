# Create your models here.
import uuid
from django.contrib.postgres.fields import ArrayField
from django.db import models


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
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
        on_delete=models.CASCADE,
        related_name="%(class)ss",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True