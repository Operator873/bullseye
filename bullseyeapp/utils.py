import datetime
import ipaddress
import json
import requests
import socket
from multiprocessing.pool import ThreadPool
from requests.exceptions import HTTPError

import shodan
from django.conf import settings
from django.contrib.gis.geoip2 import GeoIP2
from django.db.models import Model

from .models import CachedResult


def get_empty_context():
    context = {}
    context['data_sources'] = {}
    context['geoips'] = {
        'type': 'FeatureCollection',
        'features': []
    }
    return context

def has_group(key, wikidict):
    return 'groups' in wikidict and key in wikidict['groups']

def get_userrights(user):
    context = {}
    userrights = set()
    targetwikis = set()
    if not user.is_authenticated:
        context['userrights'] = userrights
        context['targetwikis'] = set(['enwiki'])
        return context
    try:
        payload = {
            'action': 'query',
            'meta': 'globaluserinfo',
            'guiuser': user.username,
            'guiprop': 'groups|merged',
            'format': 'json'
        }
        r = requests.get('https://meta.wikimedia.org/w/api.php', params=payload)
        result = r.json()
        targetwikis.add(result['query']['globaluserinfo']['home'])
        acctwikis = result['query']['globaluserinfo']['merged']
        for w in acctwikis:
            if has_group('checkuser', w):
                userrights.add('checkuser')
                targetwikis.add(w['wiki'])
            elif has_group('sysop', w):
                userrights.add('sysop')
                targetwikis.add(w['wiki'])
        if 'steward' in result['query']['globaluserinfo']['groups']:
            userrights.add('steward')
        if 'global-sysop' in result['query']['globaluserinfo']['groups']:
            userrights.add('global-sysop')
    except HTTPError as e:
        print(e)
    context['targetwikis'] = targetwikis
    context['userrights'] = userrights
    return context

def get_cached(ip, source):
    try:
        cached = CachedResult.objects.get(ip_addr=ip, source=source)
        if cached.updated > datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2):
            return cached.result
        return None
    except CachedResult.DoesNotExist:
        return None

def update_cached(ip, source, result):
    try:
        cached = CachedResult.objects.get(ip_addr=ip, source=source)
    except CachedResult.DoesNotExist:
        cached = CachedResult(ip_addr=ip, source=source)
    cached.result = result
    cached.save()

def get_whois_data(ip):
    result = get_cached(ip, 'whois')
    context = get_empty_context()
    if not result:
        try:
            payload = {
                'ip': ip,
                'lookup': 'true',
                'format': 'json'
            }
            r = requests.get('https://whois-referral.toolforge.org/w/gateway.py', params=payload)
            r.raise_for_status()
            result = r.json()
            update_cached(ip, 'whois', result)
        except HTTPError as e:
            print(e)
            context['data_sources']['whois'] = False
            return context

    context['whois'] = result
    context['isp'] = result['asn_description']
    context['range'] = result['asn_cidr']
    if 'geo_ipinfo' in result:
        context['location'] = result['geo_ipinfo']
    context['data_sources']['whois'] = True
    return context

def get_maxmind_data(ip):
    context = get_empty_context()
    if hasattr(settings, 'GEOIP_PATH') and settings.GEOIP_PATH:
        result = get_cached(ip, 'maxmind')
        if not result:
            try:
                g = GeoIP2()
                result = g.city(ip)
                update_cached(ip, 'maxmind', result)
            except Exception as e:
                context['data_sources']['maxmind'] = False
                return context

        context['maxmind'] = result
        context['data_sources']['maxmind'] = True
        context['geoips']['features'].append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [
                    result['longitude'],
                    result['latitude']
                ]
            },
            'properties': {
                'description': 'Maxmind GeoLite2',
                'color': 'blue'
            }
        })
        context['location'] = f'{result["city"]}, {result["region"]}, {result["country_name"]}'
    else:
        context['data_sources']['maxmind'] = False
    return context

def lookup_maxmind_dartboard(ip):
    try:
        g = GeoIP2()
        result = g.city(ip)
    except Exception as e:
        print(e)
        return None
    return {
        'type': 'Feature',
        'geometry': {
            'type': 'Point',
            'coordinates': [
                result['longitude'],
                result['latitude']
            ]
        },
        'properties': {
            'description': f'{ip} (Maxmind GeoLite2)',
            'color': 'blue'
        }
    }

def get_ipcheck_data(ip):
    context = get_empty_context()
    if hasattr(settings, 'IPCHECK_KEY') and settings.IPCHECK_KEY:
        result = get_cached(ip, 'ipcheck')
        if not result:
            try:
                r = requests.get(f'https://ipcheck.toolforge.org/index.php?ip={ip}&api=true&key={settings.IPCHECK_KEY}')
                r.raise_for_status()
                result = r.json()
                update_cached(ip, 'ipcheck', result)
            except HTTPError:
                context['data_sources']['ipcheck'] = False
                return context

        context['ipcheck'] = result
        context['data_sources']['ipcheck'] = True

        # Summarize the important bits
        summary = []
        if result['webhost']['result']['webhost']:
            summary.append('webhost')
        if result['proxycheck']['result']['proxy']:
            summary.append('proxy (proxycheck)')
        if 'result' in result['stopforumspam'] and result['stopforumspam']['result']['appears']:
            summary.append('on SFS blacklist')
        if not result['computeHosts']['result']['cloud'].startswith('This IP is not'):
            summary.append(f"cloud ({result['computeHosts']['result']['cloud']})")
        if result['spamcop']['result']['listed']:
            summary.append('on SpamCop blacklist')
        if result['tor']['result']['tornode']:
            summary.append('TOR node')
        context['ipcheck']['summary'] = ', '.join(summary)
    else:
        context['data_sources']['ipcheck'] = False

    return context


def get_spur_data(ip):
    context = get_empty_context()
    if hasattr(settings, 'SPUR_KEY') and settings.SPUR_KEY:
        result = get_cached(ip, 'spur')
        if not result:
            try:
                r = requests.get(f'https://api.spur.us/v1/context/{ip}', headers={'Token': settings.SPUR_KEY})
                r.raise_for_status()
                result = r.json()
                update_cached(ip, 'spur', result)
            except HTTPError as e:
                print(e)
                context['data_sources']['spur'] = False
                return context
        context['spur'] = result
        context['data_sources']['spur'] = True
        if 'geoPrecision' in result and result['geoPrecision']['exists']:
            context['geoips']['features'].append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [
                        result['geoPrecision']['point']['longitude'],
                        result['geoPrecision']['point']['latitude']
                    ]
                },
                'properties': {
                    'description': 'Spur (usage location)',
                    'color': 'red'
                }
            })
        summary = []
        if result['vpnOperators']['exists']:
            summary.append('VPN')
            # Prettify
            context['spur']['vpns'] = ', '.join([x['name'] for x in result['vpnOperators']['operators']])

        if result['deviceBehaviors']['exists']:
            context['spur']['behaviors'] = ', '.join([x['name'] for x in result['deviceBehaviors']['behaviors']])

        if result['proxiedTraffic']['exists']:
            summary.append('callback proxy')
            # Prettify
            context['spur']['proxies'] = ', '.join([f'{x["name"]} ({x["type"]})' for x in result['proxiedTraffic']['proxies']])

        if result['wifi']['exists']:
            summary.append('wifi')
            # Prettify
            context['spur']['ssids'] = ', '.join(result['wifi']['ssids'])

        context['spur']['summary'] = ', '.join(summary)

    else:
        context['data_sources']['spur'] = False
    return context


def get_shodan_data(ip):
    context = get_empty_context()
    if hasattr(settings, 'SHODAN_KEY') and settings.SHODAN_KEY:
        result = get_cached(ip, 'shodan')
        if not result:
            try:
                api = shodan.Shodan(settings.SHODAN_KEY)
                result = api.host(ip)
                update_cached(ip, 'shodan', result)
            except Exception as e:
                print(e)
                context['data_sources']['shodan'] = False
                return context
        context['shodan'] = result
        context['data_sources']['shodan'] = True
        if 'isp' in result:
            context['isp'] = result['isp']

        if 'longitude' in result and 'latitude' in result:
            context['geoips']['features'].append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [
                        result['longitude'],
                        result['latitude']
                    ]
                },
                'properties': {
                    'description': 'Shodan',
                    'color': 'orange'
                }
            })
        context['shodan']['open_ports'] = ', '.join([str(x) for x in result['ports']])
        context['shodan']['host_list'] = ', '.join(result['hostnames'])
        context['shodan']['domain_list'] = ', '.join(result['domains'])
        summary = []

        if 80 in result['ports'] or 443 in result['ports']:
            summary.append('webhost')
        if 1194 in result['ports']:
            summary.append('OpenVPN')
        if 3128 in result['ports']:
            summary.append('squid')

        context['shodan']['summary'] = ', '.join(summary)
    else:
        context['data_sources']['shodan'] = False
    return context

def get_sitematrix():
    sitematrix = get_cached('127.0.0.1', 'sitematrix')
    # Get/update sitematrix codes
    if not sitematrix:
        try:
            payload = {
                'action': 'sitematrix',
                'format': 'json'
            }
            r = requests.get('https://meta.wikimedia.org/w/api.php', params=payload)
            r.raise_for_status()
            sitematrix = r.json()
            update_cached('127.0.0.1', 'sitematrix', sitematrix)
        except HTTPError as e:
            print(e)
            return
    return sitematrix

def get_relevant_blocks(ip, wiki_list):
    print(wiki_list)
    context = get_empty_context()
    matrix = get_sitematrix()
    context['blocks'] = {}
    with ThreadPool() as pool:
        # Global blocks
        gblock_query = pool.apply_async(get_globalblockstatus, (ip,))

        localblock_queries = []
        # Local blocks
        for entry in matrix['sitematrix']:
            if entry in ['specials', 'count']:
                continue
            for site in matrix['sitematrix'][entry]['site']:
                if site['dbname'] in wiki_list:
                    localblock_queries.append((site['dbname'], pool.apply_async(get_blockstatus, (ip, site))))
        for site in matrix['sitematrix']['specials']:
            if site['dbname'] in wiki_list:
                localblock_queries.append((site['dbname'], pool.apply_async(get_blockstatus, (ip, site))))

        for query in localblock_queries:
            context['blocks'][query[0]] = query[1].get()

        context['globalblocks'] = gblock_query.get()

    summary = []
    if context['globalblocks']:
        summary.append('global block')
    for (wiki, block) in context['blocks'].items():
        if not block:
            continue
        blocktype = 'block'
        for blockentry in block:
            if not blockentry['anononly']:
                blocktype = 'hardblock'
        summary.append(f'{wiki} {blocktype}')

        context['blocksummary'] = ', '.join(summary)
    return context

def get_blockstatus(ip, wiki):
    url = wiki['url']
    try:
        payload = {
            'action': 'query',
            'list': 'blocks',
            'bkip': ip,
            'formatversion': 2,
            'format': 'json'
        }
        r = requests.get(url + '/w/api.php', params=payload)
        r.raise_for_status()
        result = r.json()
        return result['query']['blocks']
    except HTTPError as e:
        print(e)
        return None

def get_globalblockstatus(ip):
    try:
        payload = {
            'action': 'query',
            'list': 'globalblocks',
            'bgip': ip,
            'bgprop': 'address|range|reason|timestamp|by|expiry',
            'format': 'json'
        }
        r = requests.get('https://meta.wikimedia.org/w/api.php', params=payload)
        r.raise_for_status()
        result = r.json()
        return result['query']['globalblocks']
    except HTTPError as e:
        print(e)

def parse_ip_form(form_text):
    ips = []
    errors = []
    for line in form_text.splitlines():
        cleaned = line.strip()
        try:
            ipaddress.ip_address(line)
            ips.append(cleaned)
        except:
            errors.append(line)
    return ips, errors

def get_rdns(ip):
    context = {}
    try:
        context['rdns'] = socket.gethostbyaddr(ip)[0]
    except socket.herror:
        context['rdns'] = 'unknown'
    return context

def get_bgpview_data(ip):
    context = get_empty_context()
    result = get_cached(ip, 'bgpview')
    if not result:
        try:
            r = requests.get(f'https://api.bgpview.io/ip/{ip}')
            r.raise_for_status()
            result = r.json()['data']
            if r.json()['status'] != 'ok':
                print(result['status_message'])
                context['data_sources']['bgpview'] = False
                return context
            update_cached(ip, 'bgpview', result)
        except Exception as e:
            print(e)
            context['data_sources']['bgpview'] = False
            return context

    context['bgpview'] = result
    print(result)
    context['isp'] = result['prefixes'][0]['description']
    context['range'] = result['prefixes'][0]['prefix']
    context['data_sources']['bgpview'] = True
    return context
