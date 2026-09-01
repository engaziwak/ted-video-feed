import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

# 1. Initialize the Podcast Feed
fg = FeedGenerator()
fg.title('TED Talks Daily (Custom Video)')
fg.link(href='https://www.ted.com/talks', rel='alternate')
fg.description('Custom TED video feed generated via GitHub Actions')
fg.load_extension('podcast')

# 2. Add a dummy entry to test the pipeline (Replace with real scraping logic later)
fe = fg.add_entry()
fe.id('ted-test-123')
fe.title('Example TED Talk Setup')
fe.description('If you see this in Downcast, your GitHub pipeline is working!')
fe.enclosure('https://download.ted.com/talks/example.mp4', 0, 'video/mp4')

# 3. Save the feed to an XML file
fg.rss_file('feed.xml')
