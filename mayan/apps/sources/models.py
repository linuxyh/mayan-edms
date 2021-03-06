from __future__ import unicode_literals

from email import message_from_string
from email.Utils import collapse_rfc2231_value
from email.header import decode_header
import imaplib
import json
import logging
import os
import poplib

import yaml

from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from model_utils.managers import InheritanceManager

from common.compressed_files import CompressedFile, NotACompressedFile
from converter.literals import DIMENSION_SEPARATOR
from converter.models import Transformation
from djcelery.models import PeriodicTask, IntervalSchedule
from documents.models import Document, DocumentType
from documents.settings import setting_language
from metadata.api import save_metadata_list, set_bulk_metadata
from metadata.models import MetadataType
from tags.models import Tag

from .classes import Attachment, SourceUploadedFile, StagingFile
from .literals import (
    DEFAULT_INTERVAL, DEFAULT_POP3_TIMEOUT, DEFAULT_IMAP_MAILBOX,
    DEFAULT_METADATA_ATTACHMENT_NAME, SOURCE_CHOICES, SOURCE_CHOICE_STAGING,
    SOURCE_CHOICE_WATCH, SOURCE_CHOICE_WEB_FORM,
    SOURCE_INTERACTIVE_UNCOMPRESS_CHOICES, SOURCE_UNCOMPRESS_CHOICES,
    SOURCE_UNCOMPRESS_CHOICE_N, SOURCE_UNCOMPRESS_CHOICE_Y,
    SOURCE_CHOICE_EMAIL_IMAP, SOURCE_CHOICE_EMAIL_POP3
)

logger = logging.getLogger(__name__)


@python_2_unicode_compatible
class Source(models.Model):
    label = models.CharField(max_length=64, verbose_name=_('Label'))
    enabled = models.BooleanField(default=True, verbose_name=_('Enabled'))

    objects = InheritanceManager()

    @classmethod
    def class_fullname(cls):
        return unicode(dict(SOURCE_CHOICES).get(cls.source_type))

    def __str__(self):
        return '%s' % self.label

    def fullname(self):
        return ' '.join([self.class_fullname(), '"%s"' % self.label])

    def upload_document(self, file_object, document_type, description=None, label=None, language=None, metadata_dict_list=None, metadata_dictionary=None, tag_ids=None, user=None):
        try:
            with transaction.atomic():
                document = Document.objects.create(
                    description=description or '', document_type=document_type,
                    label=label or file_object.name,
                    language=language or setting_language.value
                )
                document.save(_user=user)

                document_version = document.new_version(
                    file_object=file_object, _user=user
                )

                if user:
                    document.add_as_recent_document_for_user(user)

                Transformation.objects.copy(
                    source=self, targets=document_version.pages.all()
                )

                if metadata_dict_list:
                    save_metadata_list(
                        metadata_dict_list, document, create=True
                    )

                if metadata_dictionary:
                    set_bulk_metadata(
                        document=document,
                        metadata_dictionary=metadata_dictionary
                    )

                if tag_ids:
                    for tag in Tag.objects.filter(pk__in=tag_ids):
                        tag.documents.add(document)

        except Exception as exception:
            logger.critical(
                'Unexpected exception while trying to create new document '
                '"%s" from source "%s"; %s',
                label or file_object.name, self, exception
            )
            raise

    def handle_upload(self, file_object, description=None, document_type=None, expand=False, label=None, language=None, metadata_dict_list=None, metadata_dictionary=None, user=None):
        if not document_type:
            document_type = self.document_type

        kwargs = {
            'description': description, 'document_type': document_type,
            'label': label, 'language': language,
            'metadata_dict_list': metadata_dict_list,
            'metadata_dictionary': metadata_dictionary, 'user': user
        }

        if expand:
            try:
                compressed_file = CompressedFile(file_object)
                for compressed_file_child in compressed_file.children():
                    kwargs.update({'label': unicode(compressed_file_child)})
                    self.upload_document(
                        file_object=File(compressed_file_child), **kwargs
                    )
                    compressed_file_child.close()

            except NotACompressedFile:
                logging.debug('Exception: NotACompressedFile')
                self.upload_document(file_object=file_object, **kwargs)
        else:
            self.upload_document(file_object=file_object, **kwargs)

    def get_upload_file_object(self, form_data):
        pass
        # TODO: Should raise NotImplementedError()?

    def clean_up_upload_file(self, upload_file_object):
        pass
        # TODO: Should raise NotImplementedError()?

    class Meta:
        ordering = ('label',)
        verbose_name = _('Source')
        verbose_name_plural = _('Sources')


class InteractiveSource(Source):
    objects = InheritanceManager()

    class Meta:
        verbose_name = _('Interactive source')
        verbose_name_plural = _('Interactive sources')


class StagingFolderSource(InteractiveSource):
    is_interactive = True
    source_type = SOURCE_CHOICE_STAGING

    folder_path = models.CharField(
        max_length=255, help_text=_('Server side filesystem path.'),
        verbose_name=_('Folder path')
    )
    preview_width = models.IntegerField(
        help_text=_('Width value to be passed to the converter backend.'),
        verbose_name=_('Preview width')
    )
    preview_height = models.IntegerField(
        blank=True, null=True,
        help_text=_('Height value to be passed to the converter backend.'),
        verbose_name=_('Preview height')
    )
    uncompress = models.CharField(
        choices=SOURCE_INTERACTIVE_UNCOMPRESS_CHOICES, max_length=1,
        help_text=_('Whether to expand or not compressed archives.'),
        verbose_name=_('Uncompress')
    )
    delete_after_upload = models.BooleanField(
        default=True,
        help_text=_(
            'Delete the file after is has been successfully uploaded.'
        ),
        verbose_name=_('Delete after upload')
    )

    def get_preview_size(self):
        dimensions = []
        dimensions.append(unicode(self.preview_width))
        if self.preview_height:
            dimensions.append(unicode(self.preview_height))

        return DIMENSION_SEPARATOR.join(dimensions)

    def get_file(self, *args, **kwargs):
        return StagingFile(staging_folder=self, *args, **kwargs)

    def get_files(self):
        try:
            for entry in sorted([os.path.normcase(f) for f in os.listdir(self.folder_path) if os.path.isfile(os.path.join(self.folder_path, f))]):
                yield self.get_file(filename=entry)
        except OSError as exception:
            logger.error(
                'Unable get list of staging files from source: %s; %s', self,
                exception
            )
            raise Exception(
                _('Unable get list of staging files: %s') % exception
            )

    def get_upload_file_object(self, form_data):
        staging_file = self.get_file(
            encoded_filename=form_data['staging_file_id']
        )
        return SourceUploadedFile(
            source=self, file=staging_file.as_file(), extra_data=staging_file
        )

    def clean_up_upload_file(self, upload_file_object):
        if self.delete_after_upload:
            try:
                upload_file_object.extra_data.delete()
            except Exception as exception:
                logger.error(
                    'Error deleting staging file: %s; %s', upload_file_object,
                    exception
                )
                raise Exception(
                    _('Error deleting staging file; %s') % exception
                )

    class Meta:
        verbose_name = _('Staging folder')
        verbose_name_plural = _('Staging folders')


class WebFormSource(InteractiveSource):
    is_interactive = True
    source_type = SOURCE_CHOICE_WEB_FORM

    # TODO: unify uncompress as an InteractiveSource field
    uncompress = models.CharField(
        choices=SOURCE_INTERACTIVE_UNCOMPRESS_CHOICES,
        help_text=_('Whether to expand or not compressed archives.'),
        max_length=1, verbose_name=_('Uncompress')
    )
    # Default path

    def get_upload_file_object(self, form_data):
        return SourceUploadedFile(source=self, file=form_data['file'])

    class Meta:
        verbose_name = _('Web form')
        verbose_name_plural = _('Web forms')


class OutOfProcessSource(Source):
    is_interactive = False

    class Meta:
        verbose_name = _('Out of process')
        verbose_name_plural = _('Out of process')


class IntervalBaseModel(OutOfProcessSource):
    interval = models.PositiveIntegerField(
        default=DEFAULT_INTERVAL,
        help_text=_('Interval in seconds between checks for new documents.'),
        verbose_name=_('Interval')
    )
    document_type = models.ForeignKey(
        DocumentType,
        help_text=_(
            'Assign a document type to documents uploaded from this source.'
        ),
        verbose_name=_('Document type')
    )
    uncompress = models.CharField(
        choices=SOURCE_UNCOMPRESS_CHOICES,
        help_text=_('Whether to expand or not, compressed archives.'),
        max_length=1, verbose_name=_('Uncompress')
    )

    def _get_periodic_task_name(self, pk=None):
        return 'check_interval_source-%i' % (pk or self.pk)

    def _delete_periodic_task(self, pk=None):
        try:
            periodic_task = PeriodicTask.objects.get(
                name=self._get_periodic_task_name(pk)
            )

            interval_instance = periodic_task.interval

            if tuple(interval_instance.periodictask_set.values_list('id', flat=True)) == (periodic_task.pk,):
                # Only delete the interval if nobody else is using it
                interval_instance.delete()
            else:
                periodic_task.delete()
        except PeriodicTask.DoesNotExist:
            logger.warning(
                'Tried to delete non existant periodic task "%s"',
                self._get_periodic_task_name(pk)
            )

    def save(self, *args, **kwargs):
        new_source = not self.pk
        super(IntervalBaseModel, self).save(*args, **kwargs)

        if not new_source:
            self._delete_periodic_task()

        interval_instance, created = IntervalSchedule.objects.get_or_create(
            every=self.interval, period='seconds'
        )
        # Create a new interval or reuse someone else's
        PeriodicTask.objects.create(
            name=self._get_periodic_task_name(),
            interval=interval_instance,
            task='sources.tasks.task_check_interval_source',
            kwargs=json.dumps({'source_id': self.pk})
        )

    def delete(self, *args, **kwargs):
        pk = self.pk
        super(IntervalBaseModel, self).delete(*args, **kwargs)
        self._delete_periodic_task(pk)

    class Meta:
        verbose_name = _('Interval source')
        verbose_name_plural = _('Interval sources')


class EmailBaseModel(IntervalBaseModel):
    host = models.CharField(max_length=128, verbose_name=_('Host'))
    ssl = models.BooleanField(default=True, verbose_name=_('SSL'))
    port = models.PositiveIntegerField(blank=True, null=True, help_text=_(
        'Typical choices are 110 for POP3, 995 for POP3 over SSL, 143 for '
        'IMAP, 993 for IMAP over SSL.'), verbose_name=_('Port')
    )
    username = models.CharField(max_length=96, verbose_name=_('Username'))
    password = models.CharField(max_length=96, verbose_name=_('Password'))
    metadata_attachment_name = models.CharField(
        default=DEFAULT_METADATA_ATTACHMENT_NAME,
        help_text=_(
            'Name of the attachment that will contains the metadata type '
            'names and value pairs to be assigned to the rest of the '
            'downloaded attachments. Note: This attachment has to be the '
            'first attachment.'
        ), max_length=128, verbose_name=_('Metadata attachment name')
    )
    subject_metadata_type = models.ForeignKey(
        MetadataType, blank=True, help_text=_(
            'Select a metadata type valid for the document type selected in '
            'which to store the email\'s subject.'
        ), null=True, related_name='email_subject',
        verbose_name=_('Subject metadata type')
    )
    from_metadata_type = models.ForeignKey(
        MetadataType, blank=True, help_text=_(
            'Select a metadata type valid for the document type selected in '
            'which to store the email\'s "from" value.'
        ), null=True, related_name='email_from',
        verbose_name=_('From metadata type')
    )
    store_body = models.BooleanField(
        default=True, help_text=_(
            'Store the body of the email as a text document.'
        ), verbose_name=_('Store email body')
    )

    def clean(self):
        if self.subject_metadata_type:
            if self.subject_metadata_type.pk not in self.document_type.metadata.values_list('metadata_type', flat=True):
                raise ValidationError(
                    {
                        'subject_metadata_type': _(
                            'Subject metadata type "%(metadata_type)s" is not '
                            'valid for the document type: %(document_type)s'
                        ) % {
                            'metadata_type': self.subject_metadata_type,
                            'document_type': self.document_type
                        }
                    }
                )

        if self.from_metadata_type:
            if self.from_metadata_type.pk not in self.document_type.metadata.values_list('metadata_type', flat=True):
                raise ValidationError(
                    {
                        'from_metadata_type': _(
                            '"From" metadata type "%(metadata_type)s" is not '
                            'valid for the document type: %(document_type)s'
                        ) % {
                            'metadata_type': self.from_metadata_type,
                            'document_type': self.document_type
                        }
                    }
                )

    # From: http://bookmarks.honewatson.com/2009/08/11/
    #   python-gmail-imaplib-search-subject-get-attachments/
    # TODO: Add lock to avoid running more than once concurrent same document
    # download
    # TODO: Use message ID for lock

    @staticmethod
    def getheader(header_text, default='ascii'):

        headers = decode_header(header_text)
        header_sections = [
            unicode(text, charset or default) for text, charset in headers
        ]
        return ''.join(header_sections)

    @staticmethod
    def process_message(source, message):
        counter = 1
        email = message_from_string(message)
        metadata_dictionary = {}

        if source.subject_metadata_type:
            metadata_dictionary[
                source.subject_metadata_type.name
            ] = EmailBaseModel.getheader(email['Subject'])

        if source.from_metadata_type:
            metadata_dictionary[
                source.from_metadata_type.name
            ] = EmailBaseModel.getheader(email['From'])

        for part in email.walk():
            disposition = part.get('Content-Disposition', 'none')
            logger.debug('Disposition: %s', disposition)

            if disposition.startswith('attachment'):
                raw_filename = part.get_filename()

                if raw_filename:
                    filename = collapse_rfc2231_value(raw_filename)
                else:
                    filename = _('attachment-%i') % counter
                    counter += 1

                logger.debug('filename: %s', filename)

                with Attachment(part, name=filename) as file_object:
                    if filename == source.metadata_attachment_name:
                        metadata_dictionary = yaml.safe_load(
                            file_object.read()
                        )
                        logger.debug(
                            'Got metadata dictionary: %s', metadata_dictionary
                        )
                    else:
                        source.handle_upload(
                            document_type=source.document_type,
                            file_object=file_object, label=filename,
                            expand=(
                                source.uncompress == SOURCE_UNCOMPRESS_CHOICE_Y
                            ), metadata_dictionary=metadata_dictionary
                        )
            else:
                logger.debug('No Content-Disposition')

                content_type = part.get_content_type()

                logger.debug('content_type: %s', content_type)

                if content_type == 'text/plain' and source.store_body:
                    content = part.get_payload(decode=True).decode(part.get_content_charset())
                    with ContentFile(content=content, name='email_body.txt') as file_object:
                        source.handle_upload(
                            document_type=source.document_type,
                            file_object=file_object,
                            expand=SOURCE_UNCOMPRESS_CHOICE_N, label='email_body.txt',
                            metadata_dictionary=metadata_dictionary
                        )

    class Meta:
        verbose_name = _('Email source')
        verbose_name_plural = _('Email sources')


class POP3Email(EmailBaseModel):
    source_type = SOURCE_CHOICE_EMAIL_POP3

    timeout = models.PositiveIntegerField(
        default=DEFAULT_POP3_TIMEOUT, verbose_name=_('Timeout')
    )

    def check_source(self):
        logger.debug('Starting POP3 email fetch')
        logger.debug('host: %s', self.host)
        logger.debug('ssl: %s', self.ssl)

        if self.ssl:
            mailbox = poplib.POP3_SSL(self.host, self.port)
        else:
            mailbox = poplib.POP3(self.host, self.port, timeout=self.timeout)

        mailbox.getwelcome()
        mailbox.user(self.username)
        mailbox.pass_(self.password)
        messages_info = mailbox.list()

        logger.debug('messages_info:')
        logger.debug(messages_info)
        logger.debug('messages count: %s', len(messages_info[1]))

        for message_info in messages_info[1]:
            message_number, message_size = message_info.split()
            logger.debug('message_number: %s', message_number)
            logger.debug('message_size: %s', message_size)

            complete_message = '\n'.join(mailbox.retr(message_number)[1])

            EmailBaseModel.process_message(
                source=self, message=complete_message
            )
            mailbox.dele(message_number)

        mailbox.quit()

    class Meta:
        verbose_name = _('POP email')
        verbose_name_plural = _('POP email')


class IMAPEmail(EmailBaseModel):
    source_type = SOURCE_CHOICE_EMAIL_IMAP

    mailbox = models.CharField(
        default=DEFAULT_IMAP_MAILBOX,
        help_text=_('IMAP Mailbox from which to check for messages.'),
        max_length=64, verbose_name=_('Mailbox')
    )

    # http://www.doughellmann.com/PyMOTW/imaplib/
    def check_source(self):
        logger.debug('Starting IMAP email fetch')
        logger.debug('host: %s', self.host)
        logger.debug('ssl: %s', self.ssl)

        if self.ssl:
            mailbox = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            mailbox = imaplib.IMAP4(self.host, self.port)

        mailbox.login(self.username, self.password)
        mailbox.select(self.mailbox)

        status, data = mailbox.search(None, 'NOT', 'DELETED')
        if data:
            messages_info = data[0].split()
            logger.debug('messages count: %s', len(messages_info))

            for message_number in messages_info:
                logger.debug('message_number: %s', message_number)
                status, data = mailbox.fetch(message_number, '(RFC822)')
                EmailBaseModel.process_message(
                    source=self, message=data[0][1]
                )
                mailbox.store(message_number, '+FLAGS', '\\Deleted')

        mailbox.expunge()
        mailbox.close()
        mailbox.logout()

    class Meta:
        verbose_name = _('IMAP email')
        verbose_name_plural = _('IMAP email')


class WatchFolderSource(IntervalBaseModel):
    source_type = SOURCE_CHOICE_WATCH

    folder_path = models.CharField(
        help_text=_('Server side filesystem path.'), max_length=255,
        verbose_name=_('Folder path')
    )

    def check_source(self):
        # Force self.folder_path to unicode to avoid os.listdir returning
        # str for non-latin filenames, gh-issue #163
        for file_name in os.listdir(unicode(self.folder_path)):
            full_path = os.path.join(self.folder_path, file_name)
            if os.path.isfile(full_path):
                with File(file=open(full_path, mode='rb')) as file_object:
                    self.handle_upload(
                        file_object=file_object,
                        expand=(self.uncompress == SOURCE_UNCOMPRESS_CHOICE_Y),
                        label=file_name
                    )
                    os.unlink(full_path)

    class Meta:
        verbose_name = _('Watch folder')
        verbose_name_plural = _('Watch folders')


class SourceLog(models.Model):
    source = models.ForeignKey(
        Source, related_name='logs', verbose_name=_('Source')
    )
    datetime = models.DateTimeField(
        auto_now_add=True, editable=False, verbose_name=_('Date time')
    )
    message = models.TextField(
        blank=True, editable=False, verbose_name=_('Message')
    )

    class Meta:
        get_latest_by = 'datetime'
        ordering = ('-datetime',)
        verbose_name = _('Log entry')
        verbose_name_plural = _('Log entries')
