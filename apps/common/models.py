
from django.conf import settings
from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        now = timezone.now()

        count = self.update(
            is_deleted=True,
            deleted_at=now,
        )

        return count, {self.model._meta.label: count}

    def hard_delete(self):
        return super().delete()


class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class AllObjectsManager(models.Manager):
    pass


class SoftDeleteModel(models.Model):
    is_deleted = models.BooleanField(default=False)

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    objects = ActiveManager.from_queryset(SoftDeleteQuerySet)()
    all_objects = AllObjectsManager.from_queryset(SoftDeleteQuerySet)()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"

    def soft_delete(self, user=None):
        if self.is_deleted:
            return

        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = user

        self.save(
            update_fields=[
                "is_deleted",
                "deleted_at",
                "deleted_by",
            ]
        )

    def restore(self):
        if not self.is_deleted:
            return

        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None

        self.save(
            update_fields=[
                "is_deleted",
                "deleted_at",
                "deleted_by",
            ]
        )

    def delete(self, using=None, keep_parents=False):
        self.soft_delete()

        return (
            1,
            {self._meta.label: 1},
        )
