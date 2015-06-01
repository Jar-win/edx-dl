#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import sys

from collections import namedtuple
from functools import partial
from multiprocessing.dummy import Pool as ThreadPool

from bs4 import BeautifulSoup as BeautifulSoup_

from six.moves.http_cookiejar import CookieJar
from six.moves.urllib.error import HTTPError, URLError
from six.moves.urllib.parse import urlencode
from six.moves.urllib.request import (
    urlopen,
    build_opener,
    install_opener,
    HTTPCookieProcessor,
    Request
)

from .compat import compat_print
from .parsing import edx_json2srt
from .utils import (
    directory_name,
    execute_command,
    get_filename_from_prefix,
    get_page_contents,
    get_page_contents_as_json,
)

# Force use of bs4 with html5lib
BeautifulSoup = lambda page: BeautifulSoup_(page, 'html5lib')

OPENEDX_SITES = {
    'edx': {
        'url': 'https://courses.edx.org',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'stanford': {
        'url': 'https://lagunita.stanford.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'usyd-sit': {
        'url': 'http://online.it.usyd.edu.au',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'fun': {
        'url': 'https://www.france-universite-numerique-mooc.fr',
        'courseware-selector': ('section', {'aria-label': 'Menu du cours'}),
    },
    'gwu-seas': {
        'url': 'http://openedx.seas.gwu.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'gwu-open': {
        'url': 'http://mooc.online.gwu.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
    'mitprox': {
        'url': 'https://mitprofessionalx.mit.edu',
        'courseware-selector': ('nav', {'aria-label': 'Course Navigation'}),
    },
}
BASE_URL = OPENEDX_SITES['edx']['url']
EDX_HOMEPAGE = BASE_URL + '/login_ajax'
LOGIN_API = BASE_URL + '/login_ajax'
DASHBOARD = BASE_URL + '/dashboard'
COURSEWARE_SEL = OPENEDX_SITES['edx']['courseware-selector']

YOUTUBE_VIDEO_ID_LENGTH = 11

#
# The next four named tuples represent the structure of courses in edX.  The
# structure is:
#
# * A Course contains Sections
# * Each Section contains Subsections
# * Each Subsection contains Units
#
# Notice that we don't represent the full tree structure for both performance
# and UX reasons:
#
# Course ->  [Section] -> [SubSection] -> [Unit]
#
# In the script the data structures used are:
#
# 1. The data structures to represent the course information:
#    Course, Section->[SubSection]
#
# 2. The data structures to represent the chosen courses and sections:
#    selections = {Course, [Section]}
#
# 3. The data structure of all the downloable resources which represent each
#    subsection via its URL and the of resources who can be extracted from the
#    Units it contains:
#    all_units = {Subsection.url: [Unit]}
#
Course = namedtuple('Course', ['id', 'name', 'url', 'state'])
Section = namedtuple('Section', ['position', 'name', 'url', 'subsections'])
SubSection = namedtuple('SubSection', ['position', 'name', 'url'])
Unit = namedtuple('Unit', ['video_youtube_url', 'available_subs_url', 'sub_template_url', 'mp4_urls', 'pdf_urls'])

def change_openedx_site(site_name):
    """
    Changes the openedx website for the given one via the key
    """
    global BASE_URL
    global EDX_HOMEPAGE
    global LOGIN_API
    global DASHBOARD
    global COURSEWARE_SEL

    if site_name not in OPENEDX_SITES.keys():
        compat_print("OpenEdX platform should be one of: %s" % ', '.join(OPENEDX_SITES.keys()))
        sys.exit(2)

    BASE_URL = OPENEDX_SITES[site_name]['url']
    EDX_HOMEPAGE = BASE_URL + '/login_ajax'
    LOGIN_API = BASE_URL + '/login_ajax'
    DASHBOARD = BASE_URL + '/dashboard'
    COURSEWARE_SEL = OPENEDX_SITES[site_name]['courseware-selector']


def _display_courses(courses):
    """
    List the courses that the user has enrolled.
    """
    compat_print('You can access %d courses' % len(courses))
    for i, course in enumerate(courses, 1):
        compat_print('%2d - %s [%s]' % (i, course.name, course.id))
        compat_print('     %s' % course.url)


def get_courses_info(url, headers):
    """
    Extracts the courses information from the dashboard.
    """
    dash = get_page_contents(url, headers)
    soup = BeautifulSoup(dash)
    courses_soup = soup.find_all('article', 'course')
    courses = []
    for course_soup in courses_soup:
        course_id = None
        course_name = course_soup.h3.text.strip()
        course_url = None
        course_state = 'Not yet'
        try:
            # started courses include the course link in the href attribute
            course_url = BASE_URL + course_soup.a['href']
            if course_url.endswith('info') or course_url.endswith('info/'):
                course_state = 'Started'
            # The id of a course in edX is composed by the path
            # {organization}/{course_number}/{course_run]
            course_id = course_soup.a['href'][9:-5]
        except KeyError:
            pass
        courses.append(Course(id=course_id,
                              name=course_name,
                              url=course_url,
                              state=course_state))
    return courses


def _get_initial_token(url):
    """
    Create initial connection to get authentication token for future
    requests.

    Returns a string to be used in subsequent connections with the
    X-CSRFToken header or the empty string if we didn't find any token in
    the cookies.
    """
    cookiejar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookiejar))
    install_opener(opener)
    opener.open(url)

    for cookie in cookiejar:
        if cookie.name == 'csrftoken':
            return cookie.value

    return ''


def get_available_sections(url, headers):
    """
    Extracts the sections and subsections from a given url
    """
    def _make_url(section_soup):  # FIXME: Extract from here and test
        return BASE_URL + section_soup.ul.find('a')['href']

    def _get_section_name(section_soup):  # FIXME: Extract from here and test
        return section_soup.h3.a.string.strip()

    def _make_subsections(section_soup):
        subsections_soup = section_soup.ul.find_all("li")
        # FIXME correct extraction of subsection.name (unicode)
        subsections = [SubSection(position=i,
                                  url=BASE_URL + s.a['href'],
                                  name=s.p.string)
                       for i, s in enumerate(subsections_soup, 1)]
        return subsections

    courseware = get_page_contents(url, headers)
    soup = BeautifulSoup(courseware)
    sections_soup = soup.find_all('div', attrs={'class': 'chapter'})

    sections = [Section(position=i,
                        name=_get_section_name(section_soup),
                        url=_make_url(section_soup),
                        subsections=_make_subsections(section_soup))
                for i, section_soup in enumerate(sections_soup, 1)]
    return sections


def edx_get_subtitle(url, headers):
    """
    Return a string with the subtitles content from the url or None if no
    subtitles are available.
    """
    try:
        json_object = get_page_contents_as_json(url, headers)
        return edx_json2srt(json_object)
    except URLError as exception:
        compat_print('[warning] edX subtitles (error:%s)' % exception.reason)
        return None
    except ValueError as exception:
        compat_print('[warning] edX subtitles (error:%s)' % exception.message)
        return None


def edx_login(url, headers, username, password):
    """
    logins user into the openedx website
    """
    post_data = urlencode({'email': username,
                           'password': password,
                           'remember': False}).encode('utf-8')
    request = Request(url, post_data, headers)
    response = urlopen(request)
    resp = json.loads(response.read().decode('utf-8'))
    return resp


def parse_args():
    """
    Parse the arguments/options passed to the program on the command line.
    """
    parser = argparse.ArgumentParser(prog='edx-dl',
                                     description='Get videos from the OpenEdX platform',
                                     epilog='For further use information,'
                                     'see the file README.md',)
    # positional
    parser.add_argument('course_urls',
                        nargs='*',
                        action='store',
                        default=[],
                        help='target course urls'
                        '(e.g., https://courses.edx.org/courses/BerkeleyX/CS191x/2013_Spring/info)')

    # optional
    parser.add_argument('-u',
                        '--username',
                        required=True,
                        action='store',
                        help='your edX username (email)')
    parser.add_argument('-p',
                        '--password',
                        required=True,
                        action='store',
                        help='your edX password')
    parser.add_argument('-f',
                        '--format',
                        dest='format',
                        action='store',
                        default=None,
                        help='format of videos to download')
    parser.add_argument('-s',
                        '--with-subtitles',
                        dest='subtitles',
                        action='store_true',
                        default=False,
                        help='download subtitles with the videos')
    parser.add_argument('-o',
                        '--output-dir',
                        action='store',
                        dest='output_dir',
                        help='store the files to the specified directory',
                        default='Downloaded')
    parser.add_argument('-x',
                        '--platform',
                        action='store',
                        dest='platform',
                        help='OpenEdX platform, currently either "edx", "stanford" or "usyd-sit"',
                        default='edx')
    parser.add_argument('-cl',
                        '--course-list',
                        dest='course_list',
                        action='store_true',
                        default=False,
                        help='list available courses')
    parser.add_argument('-sf',
                        '--section-filter',
                        dest='section_filter',
                        action='store',
                        default=None,
                        help='filters sections to be downloaded')
    parser.add_argument('-sl',
                        '--section-list',
                        dest='section_list',
                        action='store_true',
                        default=False,
                        help='list available sections')
    parser.add_argument('-yo',
                        '--youtube-options',
                        dest='youtube_options',
                        action='store',
                        default='',
                        help='list available courses without downloading')

    args = parser.parse_args()
    return args


def edx_get_headers():
    """
    Builds the openedx headers to create requests
    """
    headers = {
        'User-Agent': 'edX-downloader/0.01',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
        'Referer': EDX_HOMEPAGE,
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': _get_initial_token(EDX_HOMEPAGE),
    }
    return headers


def extract_units(url, headers):
    """
    Parses a webpage and extracts its resources e.g. video_url, sub_url, etc.
    """
    compat_print("Processing '%s'..." % url)
    page = get_page_contents(url, headers)
    units = extract_units_from_html(page)
    return units


def extract_units_from_html(page):
    """
    Extract Units from the html of a subsection webpage
    """
    # in this function we avoid using beautifulsoup for performance reasons

    # parsing html with regular expressions is really nasty, don't do this if
    # you don't need to !
    re_units = re.compile('(<div?[^>]id="seq_contents_\d+".*?>.*?<\/div>)', re.DOTALL)
    # FIXME: simplify re_video_youtube_url expression
    re_video_youtube_url = re.compile(r'data-streams=(?:&#34;|").*1.0[0]*:.{11}')
    re_sub_template_url = re.compile(r'data-transcript-translation-url=(?:&#34;|")([^"&]*)(?:&#34;|")')
    re_available_subs_url = re.compile(r'data-transcript-available-translations-url=(?:&#34;|")([^"&]*)(?:&#34;|")')
    # mp4 urls may be in two places, in the field data-sources, and as <a> refs
    # This regex tries to match all the appearances, however we exclude the ';'
    # character in the urls, since it is used to separate multiple urls in one
    # string, however ';' is a valid url name character, but it is not really
    # common.
    re_mp4_urls = re.compile(r'(?:(https?://[^;]*?\.mp4))')
    re_pdf_urls = re.compile(r'href=(?:&#34;|")([^"&]*pdf)')

    units = []
    for unit_html in re_units.findall(page):
        video_youtube_url = None
        match_video_youtube_url = re_video_youtube_url.search(unit_html)
        if match_video_youtube_url is not None:
            video_id = match_video_youtube_url.group(0)[-YOUTUBE_VIDEO_ID_LENGTH:]
            video_youtube_url = 'https://youtube.com/watch?v=' + video_id

        available_subs_url = None
        sub_template_url = None
        match_subs = re_sub_template_url.search(unit_html)
        if match_subs:
            match_available_subs = re_available_subs_url.search(unit_html)
            if match_available_subs:
                available_subs_url = BASE_URL + match_available_subs.group(1)
                sub_template_url = BASE_URL + match_subs.group(1) + "/%s?videoId=" + video_id

        mp4_urls = list(set(re_mp4_urls.findall(unit_html)))
        pdf_urls = [url
                    if url.startswith('http') or url.startswith('https')
                    else BASE_URL + url
                    for url in re_pdf_urls.findall(unit_html)]

        if video_youtube_url is not None or len(mp4_urls) > 0 or len(pdf_urls) > 0:
            units.append(Unit(video_youtube_url=video_youtube_url,
                              available_subs_url=available_subs_url,
                              sub_template_url=sub_template_url,
                              mp4_urls=mp4_urls,
                              pdf_urls=pdf_urls))

    # Try to download some extra videos which is referred by iframe
    re_extra_youtube = re.compile(r'//w{0,3}\.youtube.com/embed/([^ \?&]*)[\?& ]')
    extra_ids = re_extra_youtube.findall(page)
    for extra_id in extra_ids:
        video_youtube_url = 'https://youtube.com/watch?v=' + extra_id[:YOUTUBE_VIDEO_ID_LENGTH]
        units.append(Unit(video_youtube_url=video_youtube_url,
                          available_subs_url=None,
                          sub_template_url=None,
                          mp4_urls=[],
                          pdf_urls=[]))  # FIXME: verify subtitles

    return units


def extract_all_units(urls, headers):
    """
    Returns a dict of all the units in the selected_sections: {url, units}
    """
    # for development purposes you may want to uncomment this line
    # to test serial execution, and comment all the pool related ones
    # units = [extract_units(url, headers) for url in urls]
    mapfunc = partial(extract_units, headers=headers)
    pool = ThreadPool(20)
    units = pool.map(mapfunc, urls)
    pool.close()
    pool.join()

    all_units = dict(zip(urls, units))
    return all_units


def _display_sections_menu(course, sections):
    """
    List the weeks for the given course.
    """
    num_sections = len(sections)
    compat_print('%s [%s] has %d sections so far' % (course.name, course.id, num_sections))
    for i, section in enumerate(sections, 1):
        compat_print('%2d - Download %s videos' % (i, section.name))


def _filter_sections(index, sections):
    """
    Get the sections for the given index, if the index is not valid chooses all
    """
    num_sections = len(sections)
    if index is not None:
        try:
            index = int(index)
            if index > 0 and index <= num_sections:
                return [sections[index - 1]]
        except ValueError:
            pass
    return sections


def _display_sections(sections):
    """
    Displays a tree of section(s) and subsections
    """
    compat_print('Downloading %d section(s)' % len(sections))
    for section in sections:
        compat_print('Section %2d: %s' % (section.position, section.name))
        for subsection in section.subsections:
            compat_print('  %s' % subsection.name)


def parse_courses(args, available_courses):
    """
    Parses courses options and returns the selected_courses
    """
    if args.course_list:
        _display_courses(available_courses)
        exit(0)

    if len(args.course_urls) == 0:
        compat_print('You must pass the URL of at least one course, check the correct url with --course-list')
        exit(3)

    selected_courses = [available_course
                        for available_course in available_courses
                        for url in args.course_urls
                        if available_course.url == url]
    if len(selected_courses) == 0:
        compat_print('You have not passed a valid course url, check the correct url with --course-list')
        exit(4)
    return selected_courses


def parse_sections(args, selections):
    """
    Parses sections options and returns selections filtered by
    selected_sections
    """
    if args.section_list:
        for selected_course, selected_sections in selections.items():
            _display_sections_menu(selected_course, selected_sections)
        exit(0)

    if not args.section_filter:
        return selections

    filtered_selections = {selected_course:
                           _filter_sections(args.section_filter, selected_sections)
                           for selected_course, selected_sections in selections.items()}
    return filtered_selections


def _display_selections(selections):
    """
    Displays the course, sections and subsections to be downloaded
    """
    for selected_course, selected_sections in selections.items():
        compat_print('Downloading %s [%s]' % (selected_course.name,
                                              selected_course.id))
        _display_sections(selected_sections)


def parse_units(all_units):
    """
    Parses units options and corner cases
    """
    flat_units = [unit for units in all_units.values() for unit in units]
    if len(flat_units) < 1:
        compat_print('WARNING: No downloadable video found.')
        exit(6)


def _download_video_youtube(unit, args, target_dir, filename_prefix):
    """
    Downloads the url in unit.video_youtube_url using youtube-dl
    """
    if unit.video_youtube_url is not None:
        BASE_EXTERNAL_CMD = ['youtube-dl', '--ignore-config']
        filename = filename_prefix + "-%(title)s-%(id)s.%(ext)s"
        fullname = os.path.join(target_dir, filename)
        video_format_option = args.format + '/mp4' if args.format else 'mp4'

        cmd = BASE_EXTERNAL_CMD + ['-o', fullname, '-f',
                                   video_format_option]
        if args.subtitles:
            cmd.append('--all-subs')
        cmd.extend(args.youtube_options.split())
        cmd.append(unit.video_youtube_url)
        execute_command(cmd)


def _download_subtitles(unit, target_dir, filename_prefix, headers):
    """
    Downloads the subtitles using the openedx subtitle api
    """
    filename = get_filename_from_prefix(target_dir, filename_prefix)
    if filename is None:
        compat_print('[warning] no video downloaded for %s' % filename_prefix)
        return
    if unit.sub_template_url is None:
        compat_print('[warning] no subtitles downloaded for %s' % filename_prefix)
        return

    try:
        available_subs = get_page_contents_as_json(unit.available_subs_url,
                                                   headers)
    except HTTPError:
        available_subs = ['en']

    for sub_lang in available_subs:
        sub_url = unit.sub_template_url % sub_lang
        subs_filename = os.path.join(target_dir,
                                     filename + '.' + sub_lang + '.srt')
        if not os.path.exists(subs_filename):
            subs_string = edx_get_subtitle(sub_url, headers)
            if subs_string:
                compat_print('[info] Writing edX subtitle: %s' % subs_filename)
                open(os.path.join(os.getcwd(), subs_filename),
                     'wb+').write(subs_string.encode('utf-8'))
        else:
            compat_print('[info] Skipping existing edX subtitle %s' % subs_filename)


def _download_urls(urls, target_dir, filename_prefix):
    for url in urls:
        original_filename = url.rsplit('/', 1)[1]
        filename = os.path.join(target_dir, filename_prefix + '-' + original_filename)
        _print('[download] Destination: %s' % filename)
        urlretrieve(url, filename)


def download_unit(unit, args, target_dir, filename_prefix, headers):
    """
    Downloads unit based on args in the given target_dir with filename_prefix
    """
    mkdir_p(target_dir)

    _download_video_youtube(unit, args, target_dir, filename_prefix)

    if args.subtitles:
        _download_subtitles(unit, target_dir, filename_prefix, headers)

    if len(unit.mp4_urls) > 0:
        _download_urls(unit.mp4_urls, target_dir, filename_prefix)

    if len(unit.pdf_urls) > 0:
        _download_urls(unit.pdf_urls, target_dir, filename_prefix)


def download(args, selections, all_units, headers):
    """
    Downloads all the resources based on the selections
    """
    compat_print("[info] Output directory: " + args.output_dir)
    # Download Videos
    # notice that we could iterate over all_units, but we prefer to do it over
    # sections/subsections to add correct prefixes and shows nicer information
    for selected_course, selected_sections in selections.items():
        coursename = directory_name(selected_course.name)
        for selected_section in selected_sections:
            section_dirname = "%02d-%s" % (selected_section.position,
                                           selected_section.name)
            target_dir = os.path.join(args.output_dir, coursename,
                                      section_dirname)
            counter = 0
            for subsection in selected_section.subsections:
                units = all_units.get(subsection.url, [])
                for unit in units:
                    counter += 1
                    filename_prefix = "%02d" % counter
                    download_unit(unit, args, target_dir, filename_prefix,
                                  headers)


def remove_repeated_video_urls(all_units):
    """
    Removes repeated video_urls from the selections, this avoids repeated
    video downloads
    """
    existing_urls = set()
    filtered_units = {}
    for url, units in all_units.items():
        reduced_units = []
        for unit in units:
            if unit.video_youtube_url not in existing_urls:
                reduced_units.append(unit)
                existing_urls.add(unit.video_youtube_url)
        filtered_units[url] = reduced_units
    return filtered_units


def _length_units(all_units):
    """
    Counts the number of units in a all_units dict
    """
    counter = 0
    for _, units in all_units.items():
        for _ in units:
            counter += 1
    return counter


def main():
    """
    Main program function
    """
    args = parse_args()

    change_openedx_site(args.platform)

    if not args.username or not args.password:
        compat_print("You must supply username and password to log-in")
        exit(1)

    # Prepare Headers
    headers = edx_get_headers()

    # Login
    resp = edx_login(LOGIN_API, headers, args.username, args.password)
    if not resp.get('success', False):
        compat_print(resp.get('value', "Wrong Email or Password."))
        exit(2)

    # Parse and select the available courses
    courses = get_courses_info(DASHBOARD, headers)
    available_courses = [course for course in courses if course.state == 'Started']
    selected_courses = parse_courses(args, available_courses)

    # Parse the sections and build the selections dict filtered by sections
    all_selections = {selected_course:
                      get_available_sections(selected_course.url.replace('info', 'courseware'), headers)
                      for selected_course in selected_courses}
    selections = parse_sections(args, all_selections)
    _display_selections(selections)

    # Extract the unit information (downloadable resources)
    # This parses the HTML of all the subsection.url and extracts
    # the URLs of the resources as Units.
    all_urls = [subsection.url
                for selected_sections in selections.values()
                for selected_section in selected_sections
                for subsection in selected_section.subsections]
    all_units = extract_all_units(all_urls, headers)
    parse_units(selections)

    # This removes all repeated video_urls
    # FIXME: This is not the best way to do it but it is the simplest, a
    # better approach will be to create symbolic or hard links for the repeated
    # units to avoid losing information
    filtered_units = remove_repeated_video_urls(all_units)
    num_all_units = _length_units(all_units)
    num_filtered_units = _length_units(filtered_units)
    compat_print('Removed %d units from total %d' % (num_all_units - num_filtered_units,
                                                     num_all_units))

    # finally we download all the resources
    download(args, selections, all_units, headers)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        compat_print("\n\nCTRL-C detected, shutting down....")
        sys.exit(0)
