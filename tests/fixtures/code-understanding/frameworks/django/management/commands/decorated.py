from django.core.management.base import BaseCommand
from ...views import decorate


class Command(BaseCommand):
    @decorate
    def handle(self, *args, **options):
        return "decorated"
