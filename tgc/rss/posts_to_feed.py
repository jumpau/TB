import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from dateutil import parser
from feedgen.feed import FeedGenerator
from markdown import markdown


@dataclass
class FeedMeta:
    title: str
    link: str
    description: str
    language: str
    image_url: str


@dataclass
class SitemapMeta:
    base_url: str
    default_changefreq: str = "weekly"
    default_priority: float = 0.5


def posts_to_feed(path: Path, meta: FeedMeta, posts_data=None):
    """
    Convert posts to RSS feed. This function will create the rss feed in the same directory as posts.json

    :param path: Path to the parent directory that contains posts.json
    :param meta: Feed meta info
    :param posts_data: Optional posts data to use instead of reading from posts.json
    """
    fg = FeedGenerator()
    
    # Meta info
    fg.id(meta.link)
    fg.title(meta.title)
    fg.link(href=meta.link, rel='alternate')
    fg.description(meta.description)
    fg.language(meta.language)
    fg.image(meta.image_url)

    # Posts - 使用传入的数据或读取文件
    if posts_data is not None:
        posts = posts_data
        print(f"Using provided posts data with {len(posts)} posts")
    else:
        posts = json.loads((path / 'posts.json').read_text())
        print(f"Reading posts from {path / 'posts.json'} with {len(posts)} posts")
    for post in posts:
        fe = fg.add_entry()
        fe.id(str(post['id']))
        fe.title(f"{meta.title} #{post['id']}")
        fe.link(href=f'{meta.link}?post={post["id"]}')
        fe.updated(parser.parse(post['date']).replace(tzinfo=timezone.utc))

        # Escape HTML tags
        # text = html2text(markdown(post.get('text') or post.get('caption') or ''), bodywidth=0)
        # text = html.escape(text.replace('\n', '<br>'))
        text = markdown(post.get('text') or post.get('caption') or '')
        fe.description(text)

    fg.rss_file(path / 'rss.xml', pretty=True)
    fg.atom_file(path / 'atom.xml', pretty=True)
    
    print(f"Generated RSS and Atom feeds with {len(posts)} posts in chronological order")


def posts_to_sitemap_from_rss(path: Path, rss_meta: FeedMeta, posts_data=None, changefreq: str = "weekly", priority: float = 0.5):
    """
    Convert posts to XML sitemap using RSS feed configuration.
    
    :param path: Path to the parent directory that contains posts.json
    :param rss_meta: RSS feed meta info (will use link as base_url)
    :param posts_data: Optional posts data to use instead of reading from posts.json
    :param changefreq: Default change frequency for sitemap entries
    :param priority: Default priority for sitemap entries
    """
    # Create sitemap meta from RSS meta
    sitemap_meta = SitemapMeta(
        base_url=rss_meta.link,
        default_changefreq=changefreq,
        default_priority=priority
    )
    
    return posts_to_sitemap(path, sitemap_meta, posts_data)


def posts_to_sitemap(path: Path, meta: SitemapMeta, posts_data=None):
    """
    Convert posts to XML sitemap compatible with Google and other search engines.
    
    :param path: Path to the parent directory that contains posts.json
    :param meta: Sitemap meta info
    :param posts_data: Optional posts data to use instead of reading from posts.json
    """
    
    # Create root element
    urlset = ET.Element("urlset")
    urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")
    
    # Posts - 使用传入的数据或读取文件
    if posts_data is not None:
        posts = posts_data
        print(f"Using provided posts data with {len(posts)} posts for sitemap")
    else:
        posts = json.loads((path / 'posts.json').read_text())
        print(f"Reading posts from {path / 'posts.json'} with {len(posts)} posts for sitemap")
    
    # Add main page
    main_url = ET.SubElement(urlset, "url")
    ET.SubElement(main_url, "loc").text = meta.base_url
    ET.SubElement(main_url, "lastmod").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    ET.SubElement(main_url, "changefreq").text = "daily"
    ET.SubElement(main_url, "priority").text = "1.0"
    
    # Add each post
    for post in posts:
        url_elem = ET.SubElement(urlset, "url")
        
        # URL
        post_url = f"{meta.base_url.rstrip('/')}?post={post['id']}"
        ET.SubElement(url_elem, "loc").text = post_url
        
        # Last modified (从post的日期)
        try:
            post_date = parser.parse(post['date']).replace(tzinfo=timezone.utc)
            lastmod = post_date.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            ET.SubElement(url_elem, "lastmod").text = lastmod
        except:
            # 如果解析失败，使用当前时间
            ET.SubElement(url_elem, "lastmod").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        
        # Change frequency
        ET.SubElement(url_elem, "changefreq").text = meta.default_changefreq
        
        # Priority (文章优先级略低于主页)
        ET.SubElement(url_elem, "priority").text = str(meta.default_priority)
    
    # Write sitemap.xml
    tree = ET.ElementTree(urlset)
    ET.indent(tree, space="  ", level=0)  # Pretty print
    
    sitemap_path = path / 'sitemap.xml'
    tree.write(sitemap_path, encoding='utf-8', xml_declaration=True)
    
    print(f"Generated sitemap.xml with {len(posts) + 1} URLs (1 main page + {len(posts)} posts)")
    print(f"Sitemap saved to: {sitemap_path}")
    
    return sitemap_path


def generate_robots_txt(path: Path, base_url: str, sitemap_url: str):
    """
    Generate robots.txt file for search engine optimization.
    
    :param path: Path to save robots.txt
    :param base_url: Base URL of the website
    :param sitemap_url: URL of the sitemap
    """
    
    robots_content = [
        "User-agent: *",
        "Allow: /",
        "",
        "# Crawl-delay to be respectful to server resources", 
        "Crawl-delay: 1",
        "",
        f"Sitemap: {sitemap_url}",
        "",
        "# Common paths to disallow",
        "Disallow: /private/",
        "Disallow: /tmp/",
        "Disallow: /*.json$",
        "Disallow: /*?debug=*"
    ]
    
    # Write robots.txt
    robots_path = path / 'robots.txt'
    with open(robots_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(robots_content))
    
    print(f"Generated robots.txt at: {robots_path}")
    return robots_path
