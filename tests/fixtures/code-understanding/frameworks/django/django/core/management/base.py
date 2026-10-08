"""Synthetic framework identity, not a copy of Django."""


class BaseCommand:
    def handle(self, *args, **options):
        raise NotImplementedError
