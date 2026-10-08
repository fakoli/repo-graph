from django.core.management.missing import BaseCommand


class Command(BaseCommand):
    def handle(self, *args, **options):
        return "missing"
