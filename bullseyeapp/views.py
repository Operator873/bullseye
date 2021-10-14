import ipaddress

from deepmerge import always_merger
from multiprocessing.pool import ThreadPool

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import utils


def get_landing_page(request):
    return render(request, 'bullseye/index.html')

@require_http_methods(['GET', 'POST'])
def dartboard(request):
    if request.method == 'GET':
        return render(request, 'bullseye/dartboard.html')
    # guaranteed to be POST now
    context = {}
    context['cdnjs'] = settings.CDNJS
    if hasattr(settings, 'MAPSERVER') and settings.MAPSERVER:
        context['mapserver'] = settings.MAPSERVER
    form_data = request.POST
    geos = []
    ips, errors = utils.parse_ip_form(form_data['dartboardIPs'])
    print(ips, errors)
    context['errors'] = errors
    for ip in ips:
        if hasattr(settings, 'GEOIP_PATH') and settings.GEOIP_PATH:
            geos.append(utils.lookup_maxmind_dartboard(ip))

    context['geoips'] = {
        'type': 'FeatureCollection',
        'features': geos
    }
    context['ips'] = ips
    context['errors'] = errors
    return render(request, 'bullseye/dartboard-map.html', context)


def get_ip_info(request, ip):
    if not request.user.is_authenticated:
        return render(request, 'bullseye/notauthed.html')
    context = {}
    context['ip'] = ip
    context['data_sources'] = {}
    context['geoips'] = {
        'type': 'FeatureCollection',
        'features': []
    }

    with ThreadPool() as pool:
        queries = []
        # TODO: cache this in the User model somehow
        userrights_query = pool.apply_async(utils.get_userrights, (request.user,))

        queries.append(pool.apply_async(utils.get_whois_data, (ip,)))
        queries.append(pool.apply_async(utils.get_maxmind_data, (ip,)))
        queries.append(pool.apply_async(utils.get_rdns, (ip,)))
        queries.append(pool.apply_async(utils.get_ipcheck_data, (ip,)))
        queries.append(pool.apply_async(utils.get_bgpview_data, (ip,)))
    
        # Need the wiki query to finish to get the target wikis and access rights
        userrights_ctx = userrights_query.get()
        always_merger.merge(context, userrights_ctx)
        queries.append(pool.apply_async(utils.get_relevant_blocks, (ip, context['targetwikis'])))
    
        spur_authorized_groups = set(['steward', 'checkuser'])
        if spur_authorized_groups.intersection(set(context['userrights'])) or\
                request.user.groups.filter(name='trusted').exists():
            queries.append(pool.apply_async(utils.get_spur_data, (ip,)))
    
        shodan_authorized_groups = set(['sysop', 'global-sysop', 'steward', 'checkuser'])
        if shodan_authorized_groups.intersection(set(context['userrights'])) or\
                request.user.groups.filter(name='trusted').exists():
            queries.append(pool.apply_async(utils.get_shodan_data, (ip,)))

        for query in queries:
            query_ctx = query.get()
            always_merger.merge(context, query_ctx)

        
    context['cdnjs'] = settings.CDNJS
    if hasattr(settings, 'MAPSERVER') and settings.MAPSERVER:
        context['mapserver'] = settings.MAPSERVER
    return render(request, 'bullseye/ip.html', context)

def get_ip_range_info(request, ip, cidr):
    return HttpResponse(f'Details of {ip}/{cidr}')

def logout_view(request):
    logout(request)
    return redirect(get_landing_page)
