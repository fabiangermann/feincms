# ------------------------------------------------------------------------
# coding=utf-8
# ------------------------------------------------------------------------

from __future__ import absolute_import

from datetime import datetime
import logging
import os
import re

# Try to import PIL in either of the two ways it can end up installed.
try:
    from PIL import Image
except ImportError:
    import Image

from django.db import models
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext_lazy as _

from feincms import settings
from feincms.models import ExtensionsMixin
from feincms.translations import TranslatedObjectMixin, Translation, TranslatedObjectManager

# ------------------------------------------------------------------------
class CategoryManager(models.Manager):
    """
    Simple manager which exists only to supply ``.select_related("parent")``
    on querysets since we can't even __unicode__ efficiently without it.
    """
    def get_query_set(self):
        return super(CategoryManager, self).get_query_set().select_related("parent")

# ------------------------------------------------------------------------
class Category(models.Model):
    """
    These categories are meant primarily for organizing media files in the
    library.
    """

    title = models.CharField(_('title'), max_length=200)
    parent = models.ForeignKey('self', blank=True, null=True,
        related_name='children', limit_choices_to={'parent__isnull': True},
        verbose_name=_('parent'))

    slug = models.SlugField(_('slug'), max_length=150)

    class Meta:
        ordering = ['parent__title', 'title']
        verbose_name = _('category')
        verbose_name_plural = _('categories')

    objects = CategoryManager()

    def __unicode__(self):
        if self.parent_id:
            return u'%s - %s' % (self.parent.title, self.title)

        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)

        super(Category, self).save(*args, **kwargs)

    def path_list(self):
        if self.parent is None:
            return [ self ]
        p = self.parent.path_list()
        p.append(self)
        return p

    def path(self):
        return ' - '.join((f.title for f in self.path_list()))

# ------------------------------------------------------------------------
class MediaFileBase(models.Model, ExtensionsMixin, TranslatedObjectMixin):
    """
    Abstract media file class. Includes the :class:`feincms.models.ExtensionsMixin`
    because of the (handy) extension mechanism.
    """

    file = models.FileField(_('file'), max_length=255, upload_to=settings.FEINCMS_MEDIALIBRARY_UPLOAD_TO)
    type = models.CharField(_('file type'), max_length=12, editable=False, choices=())
    created = models.DateTimeField(_('created'), editable=False, default=datetime.now)
    copyright = models.CharField(_('copyright'), max_length=200, blank=True)
    file_size  = models.IntegerField(_("file size"), blank=True, null=True, editable=False)

    categories = models.ManyToManyField(Category, verbose_name=_('categories'),
                                        blank=True, null=True)
    categories.category_filter = True

    class Meta:
        abstract = True
        ordering = ['-created']
        verbose_name = _('media file')
        verbose_name_plural = _('media files')

    objects = TranslatedObjectManager()

    filetypes = [ ]
    filetypes_dict = { }

    @classmethod
    def reconfigure(cls, upload_to=None, storage=None):
        f = cls._meta.get_field('file')
        # Ugh. Copied relevant parts from django/db/models/fields/files.py
        # FileField.__init__ (around line 225)
        if storage:
            f.storage = storage
        if upload_to:
            f.upload_to = upload_to
            if callable(upload_to):
                f.generate_filename = upload_to

    @classmethod
    def register_filetypes(cls, *types):
        cls.filetypes[0:0] = types
        choices = [ t[0:2] for t in cls.filetypes ]
        cls.filetypes_dict = dict(choices)
        cls._meta.get_field('type').choices[:] = choices

    def __init__(self, *args, **kwargs):
        super(MediaFileBase, self).__init__(*args, **kwargs)
        if self.file:
            self._original_file_name = self.file.name

    def __unicode__(self):
        trans = None

        try:
            trans = self.translation
        except models.ObjectDoesNotExist:
            pass
        except AttributeError:
            pass

        if trans:
            trans = unicode(trans)
            if trans.strip():
                return trans
        return os.path.basename(self.file.name)

    def get_absolute_url(self):
        return self.file.url

    def determine_file_type(self, name):
        """
        >>> t = MediaFileBase()
        >>> t.determine_file_type('foobar.jpg')
        'image'
        >>> t.determine_file_type('foobar.PDF')
        'pdf'
        >>> t.determine_file_type('foobar.jpg.pdf')
        'pdf'
        >>> t.determine_file_type('foobar.jgp')
        'other'
        >>> t.determine_file_type('foobar-jpg')
        'other'
        """
        for type_key, type_name, type_test in self.filetypes:
            if type_test(name):
                return type_key
        return self.filetypes[-1][0]

    def save(self, *args, **kwargs):
        if not self.id and not self.created:
            self.created = datetime.now()

        self.type = self.determine_file_type(self.file.name)
        if self.file:
            try:
                self.file_size = self.file.size
            except (OSError, IOError, ValueError), e:
                logging.error("Unable to read file size for %s: %s", self, e)

        # Try to detect things that are not really images
        if self.type == 'image':
            try:
                try:
                    image = Image.open(self.file)
                except (OSError, IOError):
                    image = Image.open(self.file.path)

                # Rotate image based on exif data.
                if image:
                    try:
                        exif = image._getexif()
                    except (AttributeError, IOError):
                        exif = False
                    # PIL < 1.1.7 chokes on JPEGs with minimal EXIF data and
                    # throws a KeyError deep in its guts.
                    except KeyError:
                        exif = False

                    if exif:
                        orientation = exif.get(274)
                        rotation = 0
                        if orientation == 3:
                            rotation = 180
                        elif orientation == 6:
                            rotation = 270
                        elif orientation == 8:
                            rotation = 90
                        if rotation:
                            image = image.rotate(rotation)
                            image.save(self.file.path)
            except (OSError, IOError), e:
                self.type = self.determine_file_type('***') # It's binary something

        if getattr(self, '_original_file_name', None):
            if self.file.name != self._original_file_name:
                self.file.storage.delete(self._original_file_name)

        super(MediaFileBase, self).save(*args, **kwargs)
        self.purge_translation_cache()

# ------------------------------------------------------------------------
MediaFileBase.register_filetypes(
        # Should we be using imghdr.what instead of extension guessing?
        ('image', _('Image'), lambda f: re.compile(r'\.(bmp|jpe?g|jp2|jxr|gif|png|tiff?)$', re.IGNORECASE).search(f)),
        ('video', _('Video'), lambda f: re.compile(r'\.(mov|m[14]v|mp4|avi|mpe?g|qt|ogv|wmv)$', re.IGNORECASE).search(f)),
        ('audio', _('Audio'), lambda f: re.compile(r'\.(au|mp3|m4a|wma|oga|ram|wav)$', re.IGNORECASE).search(f)),
        ('pdf', _('PDF document'), lambda f: f.lower().endswith('.pdf')),
        ('swf', _('Flash'), lambda f: f.lower().endswith('.swf')),
        ('txt', _('Text'), lambda f: f.lower().endswith('.txt')),
        ('rtf', _('Rich Text'), lambda f: f.lower().endswith('.rtf')),
        ('zip', _('Zip archive'), lambda f: f.lower().endswith('.zip')),
        ('doc', _('Microsoft Word'), lambda f: re.compile(r'\.docx?$', re.IGNORECASE).search(f)),
        ('xls', _('Microsoft Excel'), lambda f: re.compile(r'\.xlsx?$', re.IGNORECASE).search(f)),
        ('ppt', _('Microsoft PowerPoint'), lambda f: re.compile(r'\.pptx?$', re.IGNORECASE).search(f)),
        ('other', _('Binary'), lambda f: True), # Must be last
    )

# ------------------------------------------------------------------------
class MediaFile(MediaFileBase):
    @classmethod
    def register_extension(cls, register_fn):
        from .admin import MediaFileAdmin

        register_fn(cls, MediaFileAdmin)

# ------------------------------------------------------------------------
class MediaFileTranslation(Translation(MediaFile)):
    """
    Translated media file caption and description.
    """

    caption = models.CharField(_('caption'), max_length=200)
    description = models.TextField(_('description'), blank=True)

    class Meta:
        verbose_name = _('media file translation')
        verbose_name_plural = _('media file translations')

    def __unicode__(self):
        return self.caption

#-------------------------------------------------------------------------
#-------------------------------------------------------------------------
