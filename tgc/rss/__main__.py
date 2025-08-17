import argparse
import json
from pathlib import Path

import toml

from tgc.rss.posts_to_feed import FeedMeta, SitemapMeta, posts_to_feed, posts_to_sitemap, posts_to_sitemap_from_rss, generate_robots_txt

if __name__ == '__main__':
    # Create argument parser
    parser = argparse.ArgumentParser(description='Convert posts.json to RSS feed and XML sitemap')
    parser.add_argument('path', help='Path to the directory that contains posts.json', nargs='?', default='.')

    # RSS Feed arguments
    parser.add_argument('--title', help='Feed title')
    parser.add_argument('--link', help='Feed link')
    parser.add_argument('--description', help='Feed description')
    parser.add_argument('--language', help='Feed language')
    parser.add_argument('--image-url', help='Feed image URL')

    # Sitemap arguments (for separate sitemap generation)
    parser.add_argument('--base-url', help='Base URL for separate sitemap generation')

    # Supply meta info through config file
    parser.add_argument('-c', '--config', help='Path to config.toml file')
    
    # Mode selection
    parser.add_argument('--sitemap-only', action='store_true', help='Generate only sitemap (no RSS)')
    parser.add_argument('--rss-only', action='store_true', help='Generate only RSS (no auto-sitemap)')
    
    args = parser.parse_args()

    # Check if posts.json exists
    if not (Path(args.path) / 'posts.json').exists():
        print('Please execute this command in the directory that contains posts.json')
        exit(1)

    # Load config file if provided
    config = {}
    if args.config:
        config = toml.loads(Path(args.config).read_text())

    # Determine what to generate
    generate_rss = not args.sitemap_only
    generate_sitemap = bool(args.base_url or config.get('sitemap')) and not args.rss_only

    # Generate RSS if requested
    if generate_rss:
        # Create RSS meta info object
        rss_config = config.get('rss', {})
        meta = FeedMeta(
            title=args.title or rss_config.get('title'),
            link=args.link or rss_config.get('link'),
            description=args.description or rss_config.get('description'),
            language=args.language or rss_config.get('language'),
            image_url=args.image_url or rss_config.get('image_url')
        )

        # Check necessary meta info
        if not meta.title or not meta.link or not meta.description or not meta.language or not meta.image_url:
            print('RSS Meta is:', meta)
            print('Please supply all necessary RSS meta info')
            if not generate_sitemap:
                exit(1)
        else:
            # Call posts_to_feed
            posts_to_feed(Path(args.path), meta)
            
            # 自动从RSS配置生成站点地图（除非明确禁用）
            if not args.rss_only:
                print("Auto-generating XML sitemap from RSS configuration...")
                posts_to_sitemap_from_rss(Path(args.path), meta)
                
                # 生成robots.txt
                sitemap_url = f"{meta.link.rstrip('/')}/sitemap.xml"
                generate_robots_txt(Path(args.path), meta.link, sitemap_url)

    # Generate sitemap if requested (separate configuration)
    if generate_sitemap:
        sitemap_config = config.get('sitemap', {})
        base_url = args.base_url or sitemap_config.get('base_url')
        
        if not base_url:
            print('Please provide base URL for separate sitemap generation')
            exit(1)
            
        sitemap_meta = SitemapMeta(
            base_url=base_url,
            default_changefreq=sitemap_config.get('default_changefreq', 'weekly'),
            default_priority=sitemap_config.get('default_priority', 0.5)
        )
        
        # Generate sitemap and robots.txt
        posts_to_sitemap(Path(args.path), sitemap_meta)
        sitemap_url = f"{base_url.rstrip('/')}/sitemap.xml"
        generate_robots_txt(Path(args.path), base_url, sitemap_url)
