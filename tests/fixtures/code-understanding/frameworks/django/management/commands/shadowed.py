from django.core.management.base import BaseCommand

BaseCommand = object


class Command(BaseCommand):
    def handle(self, *args, **options):
        return "shadowed"
