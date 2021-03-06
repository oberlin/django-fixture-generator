import os
from optparse import make_option

from django.core.management import BaseCommand, call_command, CommandError
from django.core.management.commands.dumpdata import Command as DumpDataCommand
from django.conf import settings
from django.db import router, connections
from django.utils.importlib import import_module
from django.utils.module_loading import module_has_submodule


FIXTURE_DATABASE = "__fixture_gen__"

class CircularDependencyError(Exception):
    """
    Raised when there is a circular dependency in fixture requirements.
    """
    pass

def linearize_requirements(available_fixtures, fixture, seen=None):
    if seen is None:
        seen = set([fixture])
    requirements = []
    models = []

    for requirement in fixture.requires:
        app_label, fixture_name = requirement.rsplit(".", 1)
        fixture_func = available_fixtures[(app_label, fixture_name)]
        if fixture_func in seen:
            raise CircularDependencyError
        r, m = linearize_requirements(
            available_fixtures,
            fixture_func,
            seen | set([fixture_func])
        )
        requirements.extend([req for req in r if req not in requirements])
        models.extend([model for model in m if model not in models])

    models.extend([model for model in fixture.models if model not in models])
    requirements.append(fixture)
    return requirements, models


class FixtureRouter(object):
    def __init__(self, models):
        self.models = models

    def db_for_read(self, model, instance=None, **hints):
        return FIXTURE_DATABASE

    def db_for_write(self, model, instance=None, **hints):
        return FIXTURE_DATABASE

    def allow_relation(self, *args, **kwargs):
        return True

    def allow_syncdb(self, db, model):
        return True


class Command(BaseCommand):
    option_list = tuple(
        opt for opt in DumpDataCommand.option_list
        if "--database" not in opt._long_opts and "--exclude" not in opt._long_opts
    )
    args = "app_label.fixture"

    def handle(self, fixture, **options):
        available_fixtures = {}
        for app in settings.INSTALLED_APPS:
            try:
                fixture_gen = import_module(".fixture_gen", app)
            except ImportError:
                if module_has_submodule(import_module(app), "fixture_gen"):
                   raise
                continue
            for obj in fixture_gen.__dict__.values():
                if getattr(obj, "__fixture_gen__", False):
                    available_fixtures[(app.rsplit(".", 1)[-1], obj.__name__)] = obj
        app_label, fixture_name = fixture.rsplit(".", 1)
        try:
            fixture = available_fixtures[(app_label, fixture_name)]
        except KeyError:
            available = ", ".join(
                "%s.%s" % (app_label, fixture_name)
                for app_label, fixture_name in available_fixtures
            )
            raise CommandError("Fixture generator '%s' not found, available "
                "choices: %s" % (fixture, available))

        requirements, models = linearize_requirements(available_fixtures, fixture)

        settings.DATABASES[FIXTURE_DATABASE] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
        old_routers = router.routers
        router.routers = [FixtureRouter(models)]
        try:
            # migrate_all=True is for south, Django just absorbs it
            call_command("syncdb", database=FIXTURE_DATABASE, verbosity=0,
                interactive=False, migrate_all=True)
            for fixture_func in requirements:
                fixture_func()
            call_command("dumpdata",
                *["%s.%s" % (m._meta.app_label, m._meta.object_name) for m in models],
                **dict(options, verbosity=0, database=FIXTURE_DATABASE)
            )
        finally:
            del settings.DATABASES[FIXTURE_DATABASE]
            if isinstance(connections._connections, dict):
                del connections._connections[FIXTURE_DATABASE]
            else:
                delattr(connections._connections, FIXTURE_DATABASE)
            router.routers = old_routers
