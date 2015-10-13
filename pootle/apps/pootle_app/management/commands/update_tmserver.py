#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) Pootle contributors.
#
# This file is a part of the Pootle project. It is distributed under the GPL3
# or later license. See the LICENSE file for a copy of the license and the
# AUTHORS file for copyright and authorship information.

from hashlib import md5
from optparse import make_option
import os
import sys

# This must be run before importing Django.
os.environ['DJANGO_SETTINGS_MODULE'] = 'pootle.settings'

from elasticsearch import helpers, Elasticsearch
from translate.storage import factory

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pootle_store.models import Unit


BULK_CHUNK_SIZE = 5000


class DBParser(object):

    def __init__(self, *args, **kwargs):
        super(DBParser, self).__init__(*args, **kwargs)
        self.INDEX_NAME = kwargs.pop('index', None)

    def get_units(self, *filenames):
        """Gets the units to import and its total count."""
        units_qs = Unit.simple_objects \
            .exclude(target_f__isnull=True) \
            .exclude(target_f__exact='') \
            .filter(revision__gt=self.last_indexed_revision) \
            .select_related(
                'submitted_by',
                'store',
                'store__translation_project__project',
                'store__translation_project__language'
            ).values(
                'id',
                'revision',
                'source_f',
                'target_f',
                'submitted_by__username',
                'submitted_by__full_name',
                'submitted_by__email',
                'store__translation_project__project__fullname',
                'store__pootle_path',
                'store__translation_project__language__code'
            ).order_by()

        return units_qs.iterator(), units_qs.count()

    def get_unit_data(self, unit):
        """Return dict with data to import for a single unit."""
        fullname = (unit['submitted_by__full_name'] or
                    unit['submitted_by__username'])

        email_md5 = None
        if unit['submitted_by__email']:
            email_md5 = md5(unit['submitted_by__email']).hexdigest()

        return {
            '_index': self.INDEX_NAME,
            '_type': unit['store__translation_project__language__code'],
            '_id': unit['id'],
            'revision': int(unit['revision']),
            'project': unit['store__translation_project__project__fullname'],
            'path': unit['store__pootle_path'],
            'username': unit['submitted_by__username'],
            'fullname': fullname,
            'email_md5': email_md5,
            'source': unit['source_f'],
            'target': unit['target_f'],
        }


class FileParser(object):

    def __init__(self, *args, **kwargs):
        super(FileParser, self).__init__(*args, **kwargs)
        self.INDEX_NAME = kwargs.pop('index', None)
        self.target_language = kwargs.pop('language', None)
        self.project = kwargs.pop('project', None)

    def get_units(self, *filenames):
        """Gets the units to import and its total count."""
        units = []

        for filename in filenames:
            store = factory.getobject(filename)
            if not store.gettargetlanguage() and not self.target_language:
                raise CommandError("Unable to determine target language for "
                                   "'%s'. Try again specifying a fallback "
                                   "target language with --target-language" %
                                   filename)

            self.filename = filename
            units.extend([unit for unit in store.units if unit.istranslated()])

        return units, len(units)

    def get_unit_data(self, unit):
        """Return dict with data to import for a single unit."""
        target_language = unit.gettargetlanguage()
        if target_language is None:
            target_language = self.target_language

        return {
            '_index': self.INDEX_NAME,
            '_type': target_language,
            '_id': unit.getid(),
            'revision': 0,
            'project': self.project,
            'path': self.filename,
            'username': None,
            'fullname': None,
            'email_md5': None,
            'source': unit.source,
            'target': unit.target,
        }


class Command(BaseCommand):
    help = "Load Translation Memory with translations"
    option_list = BaseCommand.option_list + (
        make_option('--refresh',
                    action='store_true',
                    dest='refresh',
                    default=False,
                    help='Process all items, not just the new ones, so '
                         'existing translations are refreshed'),
        make_option('--rebuild',
                    action='store_true',
                    dest='rebuild',
                    default=False,
                    help='Drop the entire TM on start and update everything '
                         'from scratch'),
        make_option('--dry-run',
                    action='store_true',
                    dest='dry_run',
                    default=False,
                    help='Report the number of translations to index and quit'),
        # External TM specific options.
        make_option('--tm',
                    action='store',
                    dest='tm',
                    default='local',
                    help="TM to use. TM must exist on settings. TM will be "
                         "created on the server if it doesn't exist"),
        make_option('--target-language',
                    action='store',
                    dest='target_language',
                    default='',
                    help="Target language to fallback to use in case it can't "
                         "be guessed for any of the input files."),
        make_option('--project',
                    action='store',
                    dest='project',
                    default='',
                    help='Project to use when displaying TM matches for this '
                         'translations.'),
    )

    def _parse_translations(self, *args, **options):
        units, total = self.parser.get_units(*args)

        if total == 0:
            self.stdout.write("No translations to index")
            sys.exit()

        self.stdout.write("%s translations to index" % total)

        if options['dry_run']:
            sys.exit()

        self.stdout.write("")

        for i, unit in enumerate(units, start=1):
            if (i % 1000 == 0) or (i == total):
                percent = "%.1f" % (i * 100.0 / total)
                self.stdout.write("%s (%s%%)" % (i, percent), ending='\r')
                self.stdout.flush()

            yield self.parser.get_unit_data(unit)

        if i != total:
            self.stdout.write("Expected %d, loaded %d." % (total, i))

    def _initialize(self, *args, **options):
        if not getattr(settings, 'POOTLE_TM_SERVER', False):
            raise CommandError('POOTLE_TM_SERVER setting is missing.')

        try:
            self.tm_settings = settings.POOTLE_TM_SERVER[options.get('tm')]
        except KeyError:
            raise CommandError('Specified Translation Memory is not defined '
                               'in POOTLE_TM_SERVER.')

        self.INDEX_NAME = self.tm_settings['INDEX_NAME']
        self.is_local_tm = options.get('tm') == 'local'

        self.es = Elasticsearch([{
                'host': self.tm_settings['HOST'],
                'port': self.tm_settings['PORT'],
            }],
            retry_on_timeout=True
        )

        # If files to import have been provided.
        if len(args):
            if self.is_local_tm:
                raise CommandError('You cannot add translations from files to '
                                   'a local TM.')

            self.target_language = options.pop('target_language')
            self.project = options.pop('project')

            if not self.project:
                raise CommandError('You must specify a project with '
                                   '--project.')
            self.parser = FileParser(index=self.INDEX_NAME,
                                     language=self.target_language,
                                     project=self.project)
        elif not self.is_local_tm:
            raise CommandError('You cannot add translations from database to '
                               'an external TM.')
        else:
            self.parser = DBParser(index=self.INDEX_NAME)

    def _set_latest_indexed_revision(self, **options):
        self.last_indexed_revision = -1

        if (not options['rebuild'] and
            not options['refresh'] and
            self.es.indices.exists(self.INDEX_NAME)):

            result = self.es.search(
                index=self.INDEX_NAME,
                body={
                    'query': {
                        'match_all': {}
                    },
                    'facets': {
                        'stat1': {
                            'statistical': {
                                'field': 'revision'
                            }
                        }
                    }
                }
            )
            self.last_indexed_revision = result['facets']['stat1']['max']

        self.parser.last_indexed_revision = self.last_indexed_revision

        self.stdout.write("Last indexed revision = %s" %
                          self.last_indexed_revision)

    def handle(self, *args, **options):
        self._initialize(*args, **options)

        if (options['rebuild'] and
            not options['dry_run'] and
            self.es.indices.exists(self.INDEX_NAME)):

            self.es.indices.delete(index=self.INDEX_NAME)

        if (not options['dry_run'] and
            not self.es.indices.exists(self.INDEX_NAME)):

            self.es.indices.create(index=self.INDEX_NAME)

        if self.is_local_tm:
            self._set_latest_indexed_revision(**options)

        success, _ = helpers.bulk(self.es,
                                  self._parse_translations(*args, **options))
