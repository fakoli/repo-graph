from django.db.models.manager import Manager as ModelManager
import django.db.models.manager as manager_module
from django.db import models
from .views import decorate


class ActiveManager(ModelManager):
    def get_queryset(self):
        return ()


class NamespaceManager(manager_module.Manager):
    def get_queryset(self):
        return ("namespace",)


class FacadeManager(models.Manager):
    def get_queryset(self):
        return ("facade",)


class DecoratedManager(ModelManager):
    @decorate
    def get_queryset(self):
        return ()


class FactoryManager(ModelManager.from_queryset(object)):
    def get_queryset(self):
        return ()


class PretendManager:
    def get_queryset(self):
        return ()
