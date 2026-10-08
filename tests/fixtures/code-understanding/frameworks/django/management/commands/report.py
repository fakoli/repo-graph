from django.core.management.base import BaseCommand as CommandBase


class Command(CommandBase):
    def handle(self, *args, **options):
        return "report"
