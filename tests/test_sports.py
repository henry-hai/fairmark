from fairmark.sports import series_tags, sport_icon


def test_icon_comes_from_the_kalshi_series_tag():
    series_tags.cache_clear()
    tags = {'KXLALIGAGAME': ['Soccer'], 'KXBOXING': ['Boxing'], 'KXCRICKETMATCH': ['Cricket'], 'KXODD': ['Other']}
    fetch = lambda url: {'series': {'tags': tags[url.rsplit('/', 1)[1]]}}
    assert sport_icon('KXLALIGAGAME-26OCT25BARRMA-BAR', fetch) == '⚽'
    assert sport_icon('KXBOXING-26OCT24-X', fetch) == '🥊'
    assert sport_icon('KXCRICKETMATCH-1', fetch) == '🏏'
    assert sport_icon('KXODD-1', fetch) == '🏆'


def test_lookup_failure_falls_back_to_a_trophy():
    series_tags.cache_clear()
    def fail(url):
        raise OSError('offline')
    assert sport_icon('KXNEWSPORT-1', fail) == '🏆'
