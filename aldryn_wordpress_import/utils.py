from BeautifulSoup import BeautifulSoup
from collections import defaultdict
from datetime import datetime, timedelta
import feedparser
import re
import requests
import uuid
import urllib2
from time import mktime, timezone
import StringIO
from xml.dom.minidom import parse

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.utils.html import linebreaks
from filer.models import Image

from . import factories


class WordpressParser(object):
    base_url = None
    image_placeholder = str(uuid.uuid1())

    def __init__(self, user):
        self.user = user

    def parse(self, file_path):
        if file_path is None:
            raise RuntimeError("Missing file path")

        feed = feedparser.parse(file_path)
        file_path.open()
        xml = parse(file_path)
        xmlitems = xml.getElementsByTagName("item")
        self.base_url = feed['channel']['wp_base_site_url']
        log, success, failed = [], [], []

        for (i, entry) in enumerate(feed["entries"]):
            # Get a pointer to the right position in the minidom as well.
            xmlitem = xmlitems[i]
            content = linebreaks(self.wp_caption(entry.content[0]["value"]))
            content, images = self.extract_images(content)

            # Get the time struct of the published date if possible and
            # the updated date if we can't.
            pub_date = getattr(entry, "published_parsed", entry.updated_parsed)
            pub_date = datetime.fromtimestamp(mktime(pub_date))
            pub_date -= timedelta(seconds=timezone)

            # Tags and categories are all under "tags" marked with a scheme.
            terms = defaultdict(set)
            for item in getattr(entry, "tags", []):
                terms[item.scheme].add(item.term)

            if entry.wp_post_type == "post":
                post = dict(title=entry.title, content=content,
                            publication_start=pub_date, tags=terms["tag"],
                            old_url=entry.id, images=images,
                            user=self.user)

                result, status = self.convert_to_post(post)
                if status:
                    success.append(result)
                else:
                    failed.append(result)
        log.extend(success)
        log.extend(failed)
        summary = '{} posts imported, {} failed'.format(len(success),
                                                        len(failed))
        log.append(summary)
        return '\n'.join(log)

    def wp_caption(self, post):
        """
        Filters a Wordpress Post for Image Captions and renders to
        match HTML.
        """
        for match in re.finditer(r"\[caption (.*?)\](.*?)\[/caption\]", post):
            meta = '<div '
            caption = ''
            for imatch in re.finditer(r'(\w+)="(.*?)"', match.group(1)):
                if imatch.group(1) == 'id':
                    meta += 'id="%s" ' % imatch.group(2)
                if imatch.group(1) == 'align':
                    meta += 'class="wp-caption %s" ' % imatch.group(2)
                if imatch.group(1) == 'width':
                    width = int(imatch.group(2)) + 10
                    meta += 'style="width: %spx;" ' % width
                if imatch.group(1) == 'caption':
                    caption = imatch.group(2)
            parts = (match.group(2), caption)
            meta += '>%s<p class="wp-caption-text">%s</p></div>' % parts
            post = post.replace(match.group(0), meta)
        return post

    def extract_images(self, post):
        """
        Finds direct image links. Creates filer Image objects
        and extracts links
        """

        soup = BeautifulSoup(post)
        links = soup.findAll("a")
        internal_uploads_dir = '{}/wp-content/uploads'.format(self.base_url)
        images = []
        for link in links:
            href = link['href']
            if internal_uploads_dir in href:
                if not Image.matches_file_type(href, None, None):
                    # File is not an image
                    continue
                # replace_dict = {}
                image = self.download_and_save(href)
                images.append(image)
                # Remove link from content, replace with placeholder
                link.replaceWith(self.image_placeholder)

        return str(soup), images

    def download_and_save(self, file_url):
        response = requests.get(file_url)
        file_name = urllib2.unquote(file_url).decode('utf8').split('/')[-1]
        file_extension = file_name.split('.')[-1]
        io = StringIO.StringIO()
        io.write(response.content)
        saved_file = InMemoryUploadedFile(io, None, file_name, file_extension,
                                          io.len, None)
        filer_img = Image.objects.create(original_filename=file_name,
                                         file=saved_file)
        return filer_img

    def convert_to_post(self, post_data):
        post_parts = post_data['content'].split(self.image_placeholder)
        try:
            post = factories.create_post(post_data, parts=post_parts)
            # Post already exists
        except ValueError:
            return "Post with slug {} already exists. Skipping".format(
                post_data['title']), False
        for number, part in enumerate(post_parts):
            factories.create_text_plugin(part, post.content)
            try:
                image = post_data['images'][number]
            except IndexError:
                continue
            else:
                factories.create_filer_plugin(image, post.content)

        return "Imported post {}".format(post_data['title']), True


