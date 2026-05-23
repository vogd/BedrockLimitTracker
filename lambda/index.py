"""
Bedrock Limits Tracker - Quota Publisher Lambda

Architecture:
- Runs DAILY: reads Service Quotas, publishes quota limits as CloudWatch metrics
- Real-time monitoring: CloudWatch metric math (native usage / published quota * 100)
- Dashboard uses metric math expressions for live utilization % (no Lambda in the loop)

Metrics published (static, refreshed daily):
- QuotaLimit_TPM {AccountId, Region, ModelId} = tokens-per-minute limit
- QuotaLimit_RPM {AccountId, Region, ModelId} = requests-per-minute limit

CloudWatch does the rest every minute:
- Native AWS/Bedrock Invocations metric = actual RPM
- Native AWS/Bedrock InputTokenCount + OutputTokenCount = actual TPM
- Metric math: (usage / quota) * 100 = utilization %
"""
import os
import json
import re
import boto3
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

NAMESPACE = 'BedrockLimits'
CROSS_REGION_QUOTA_MULTIPLIER = 2
MAX_THREADS = 10  # Parallel account/region processing


def handler(event, context):
    ssm = boto3.client('ssm')
    param = ssm.get_parameter(Name=os.environ['SOURCE_ACCOUNTS_PARAM'])
    account_ids = [a.strip() for a in param['Parameter']['Value'].split(',') if a.strip()]

    regions_param = os.environ.get('BEDROCK_REGIONS', 'us-east-1,us-west-2')
    regions = [r.strip() for r in regions_param.split(',')]

    all_results = []
    all_models = []  # For dashboard building

    # Process accounts/regions in parallel
    def process_account_region(account_id, region):
        data = collect_and_publish_quotas(account_id, region)
        return {
            'account': account_id, 'region': region,
            'status': 'ok',
            'models': len(data['models_with_quotas']),
            'profiles': len(data['profiles']),
            'models_with_quotas': data['models_with_quotas']
        }

    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        futures = {}
        for account_id in account_ids:
            for region in regions:
                f = executor.submit(process_account_region, account_id, region)
                futures[f] = (account_id, region)

        for f in as_completed(futures):
            account_id, region = futures[f]
            try:
                result = f.result()
                all_models.extend(result['models_with_quotas'])
                all_results.append(result)
            except Exception as e:
                print(f"ERROR {account_id}/{region}: {e}")
                all_results.append({
                    'account': account_id, 'region': region,
                    'status': 'error', 'error': str(e)
                })

    build_realtime_dashboard(all_models, all_results)
    return {'statusCode': 200, 'results': all_results}


def collect_and_publish_quotas(account_id, region):
    """Assume role, get quotas (cached), get profiles, publish quota limit metrics."""
    sts = boto3.client('sts')
    creds = sts.assume_role(
        RoleArn=f'arn:aws:iam::{account_id}:role/BedrockLimitsTrackerSpokeRole',
        RoleSessionName='BedrockLimitsTracker'
    )['Credentials']

    session = boto3.Session(
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['SecretAccessKey'],
        aws_session_token=creds['SessionToken'],
        region_name=region
    )

    # Get quotas (cached daily in S3)
    model_quotas = get_cached_quotas(account_id, region, session)

    # Get inference profiles
    bedrock = session.client('bedrock')
    profiles = list_inference_profiles(bedrock)

    # Get current usage from CloudWatch
    cw_spoke = session.client('cloudwatch')
    usage = get_model_usage(cw_spoke, profiles)

    # Build per-model effective quotas with usage
    models_with_quotas = []
    seen_models = set()

    for profile in profiles:
        model_id = profile['model_id']
        if not model_id or model_id in seen_models:
            continue
        seen_models.add(model_id)

        model_key = normalize_model_name(model_id)
        quota = None
        for qk, qv in model_quotas.items():
            if model_key in qk or qk in model_key:
                quota = qv
                break

        if not quota:
            continue

        tpm = quota.get('single_region_tpm', quota.get('cross_region_tpm', 0))
        rpm = quota.get('single_region_rpm', quota.get('cross_region_rpm', 0))
        model_usage = usage.get(model_id, {})
        tpm_used = model_usage.get('tpm', 0)
        rpm_used = model_usage.get('rpm', 0)
        tpm_util = (tpm_used / tpm * 100) if tpm > 0 else 0
        rpm_util = (rpm_used / rpm * 100) if rpm > 0 else 0

        if tpm > 0 or rpm > 0:
            models_with_quotas.append({
                'account': account_id,
                'region': region,
                'model_id': model_id,
                'tpm_quota': int(tpm),
                'rpm_quota': int(rpm),
                'tpm_used': int(tpm_used),
                'rpm_used': int(rpm_used),
                'tpm_util': round(tpm_util, 1),
                'rpm_util': round(rpm_util, 1),
                'profile_type': profile['type'],
                'capacity': profile['capacity'],
                'profile_id': profile['display_id'],
            })

    # Publish quota limits as CloudWatch metrics (static values)
    publish_quota_metrics(account_id, region, models_with_quotas)

    print(f"  {account_id}/{region}: {len(models_with_quotas)} models, {len(profiles)} profiles")
    return {'models_with_quotas': models_with_quotas, 'profiles': profiles}


def publish_quota_metrics(account_id, region, models_with_quotas):
    """Publish static quota limit metrics. CloudWatch retains last value."""
    cw = boto3.client('cloudwatch')
    now = datetime.now(timezone.utc)
    metric_data = []

    for m in models_with_quotas:
        dims = [
            {'Name': 'AccountId', 'Value': account_id},
            {'Name': 'Region', 'Value': region},
            {'Name': 'ModelId', 'Value': m['model_id']},
        ]
        if m['tpm_quota'] > 0:
            metric_data.append({
                'MetricName': 'QuotaLimit_TPM',
                'Dimensions': dims,
                'Value': m['tpm_quota'],
                'Unit': 'Count',
                'Timestamp': now
            })
        if m['rpm_quota'] > 0:
            metric_data.append({
                'MetricName': 'QuotaLimit_RPM',
                'Dimensions': dims,
                'Value': m['rpm_quota'],
                'Unit': 'Count',
                'Timestamp': now
            })

    # Publish in batches of 25
    for i in range(0, len(metric_data), 25):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data[i:i+25])


def build_realtime_dashboard(all_models, results):
    """Build dashboard with metric math for real-time utilization."""
    cw = boto3.client('cloudwatch')
    region = os.environ.get('AWS_REGION', 'us-east-1')
    now = datetime.now(timezone.utc)

    # Publish summary metrics for number widgets
    total_profiles = sum(r.get('profiles', 0) for r in results if r.get('status') == 'ok')
    over_50_tpm = sum(1 for m in all_models if m.get('tpm_util', 0) > 50)
    over_75_tpm = sum(1 for m in all_models if m.get('tpm_util', 0) > 75)
    over_50_rpm = sum(1 for m in all_models if m.get('rpm_util', 0) > 50)
    over_75_rpm = sum(1 for m in all_models if m.get('rpm_util', 0) > 75)
    cw.put_metric_data(Namespace=NAMESPACE, MetricData=[
        {'MetricName': 'TotalInferenceProfiles', 'Value': total_profiles, 'Unit': 'Count'},
        {'MetricName': 'ProfilesOver50Pct_TPM', 'Value': over_50_tpm, 'Unit': 'Count'},
        {'MetricName': 'ProfilesOver75Pct_TPM', 'Value': over_75_tpm, 'Unit': 'Count'},
        {'MetricName': 'ProfilesOver50Pct_RPM', 'Value': over_50_rpm, 'Unit': 'Count'},
        {'MetricName': 'ProfilesOver75Pct_RPM', 'Value': over_75_rpm, 'Unit': 'Count'},
    ])

    # Summary text
    accounts = sorted(set(m['account'] for m in all_models))
    regions_list = sorted(set(m['region'] for m in all_models))

    summary_md = "# Bedrock Limits Tracker (Real-Time)\n\n"
    summary_md += f"**Accounts:** {len(accounts)}  |  "
    summary_md += f"**Regions:** {', '.join(regions_list)}  |  "
    summary_md += f"**Models tracked:** {len(all_models)}\n\n"
    summary_md += f"*Quotas refreshed: {now.strftime('%Y-%m-%d %H:%M UTC')}  |  Usage: real-time via metric math*"

    # Build per-account quota tables with utilization
    # Assemble dashboard
    widgets = []

    # Summary
    widgets.append({
        'type': 'text',
        'x': 0, 'y': 0, 'width': 24, 'height': 2,
        'properties': {'markdown': summary_md}
    })

    # Number widgets + regions affected (y=2)
    widgets.append({
        'type': 'metric',
        'x': 0, 'y': 2, 'width': 4, 'height': 3,
        'properties': {
            'title': 'Total Profiles',
            'metrics': [[NAMESPACE, 'TotalInferenceProfiles']],
            'period': 300, 'stat': 'Maximum', 'region': region,
            'view': 'singleValue'
        }
    })
    widgets.append({
        'type': 'metric',
        'x': 4, 'y': 2, 'width': 5, 'height': 3,
        'properties': {
            'title': '⚠️ TPM >50% / >75% (every 1min)',
            'metrics': [
                [NAMESPACE, 'ProfilesOver50Pct_TPM', {'label': '>50%'}],
                [NAMESPACE, 'ProfilesOver75Pct_TPM', {'label': '>75%', 'color': '#d13212'}]
            ],
            'period': 300, 'stat': 'Maximum', 'region': region,
            'view': 'singleValue'
        }
    })
    widgets.append({
        'type': 'metric',
        'x': 9, 'y': 2, 'width': 5, 'height': 3,
        'properties': {
            'title': '⚠️ RPM >50% / >75% (every 1min)',
            'metrics': [
                [NAMESPACE, 'ProfilesOver50Pct_RPM', {'label': '>50%'}],
                [NAMESPACE, 'ProfilesOver75Pct_RPM', {'label': '>75%', 'color': '#d13212'}]
            ],
            'period': 300, 'stat': 'Maximum', 'region': region,
            'view': 'singleValue'
        }
    })

    # Regions affected
    regions_md = "### 🌍 Regions Affected\n\n"
    regions_md += "| Region | >50% TPM | >75% TPM | >50% RPM | >75% RPM |\n"
    regions_md += "|:-------|:--------:|:--------:|:--------:|:--------:|\n"
    for reg in regions_list:
        reg_models = [m for m in all_models if m['region'] == reg]
        t50 = sum(1 for m in reg_models if m.get('tpm_util', 0) > 50)
        t75 = sum(1 for m in reg_models if m.get('tpm_util', 0) > 75)
        r50 = sum(1 for m in reg_models if m.get('rpm_util', 0) > 50)
        r75 = sum(1 for m in reg_models if m.get('rpm_util', 0) > 75)
        regions_md += f"| {reg} | {t50} | {t75} | {r50} | {r75} |\n"

    widgets.append({
        'type': 'text',
        'x': 14, 'y': 2, 'width': 10, 'height': 3,
        'properties': {'markdown': regions_md}
    })

    # Metrics Insights: dynamic top 10 by actual usage (y=5)
    widgets.append({
        'type': 'metric',
        'x': 0, 'y': 5, 'width': 12, 'height': 6,
        'properties': {
            'title': '📈 Top 10 Models by RPM (Live)',
            'metrics': [[{
                'expression': 'SELECT SUM(Invocations) FROM SCHEMA("AWS/Bedrock", ModelId) GROUP BY ModelId ORDER BY SUM() DESC LIMIT 10',
                'id': 'q1', 'period': 60
            }]],
            'region': region,
            'view': 'timeSeries',
            'stacked': False
        }
    })
    widgets.append({
        'type': 'metric',
        'x': 12, 'y': 5, 'width': 12, 'height': 6,
        'properties': {
            'title': '📈 Top 10 Models by TPM (Live)',
            'metrics': [[{
                'expression': 'SELECT SUM(InputTokenCount) FROM SCHEMA("AWS/Bedrock", ModelId) GROUP BY ModelId ORDER BY SUM() DESC LIMIT 10',
                'id': 'q2', 'period': 60
            }]],
            'region': region,
            'view': 'timeSeries',
            'stacked': False
        }
    })

    # Per-account quota tables
    y_pos = 11

    # Single unified table for all accounts (sorted by utilization desc)
    all_sorted = sorted(all_models, key=lambda m: max(m.get('tpm_util', 0), m.get('rpm_util', 0)), reverse=True)

    table_md = "### All Models — sorted by utilization (refreshed every 1min, shows peak from last 5min)\n\n"
    table_md += "| Status | Account | Region | Model | Profile ID | Type | Capacity | TPM Limit | TPM Used | TPM% | RPM Limit | RPM Used | RPM% |\n"
    table_md += "|:-------|:--------|:-------|:------|:-----------|:-----|:---------|----------:|---------:|-----:|----------:|---------:|-----:|\n"
    for m in all_sorted:
        max_util = max(m.get('tpm_util', 0), m.get('rpm_util', 0))
        if max_util > 90:
            status = '🔴 RED'
        elif max_util > 70:
            status = '🟠 AMBER'
        elif max_util > 0:
            status = '🟢 GREEN'
        else:
            status = '🔵 NOT_USED'
        table_md += (
            f"| {status} "
            f"| {m['account']} "
            f"| {m['region']} "
            f"| {m['model_id'][:32]} "
            f"| {m['profile_id'][:22]} "
            f"| {'Sys' if m['profile_type'] == 'SYSTEM_DEFINED' else 'App'} "
            f"| {m['capacity'][:5]} "
            f"| {m['tpm_quota']:,} "
            f"| {m.get('tpm_used',0):,} "
            f"| {m.get('tpm_util',0):.0f}% "
            f"| {m['rpm_quota']:,} "
            f"| {m.get('rpm_used',0):,} "
            f"| {m.get('rpm_util',0):.0f}% |\n"
        )

    widgets.append({
        'type': 'text',
        'x': 0, 'y': y_pos, 'width': 24, 'height': 20,
        'properties': {'markdown': table_md}
    })

    cw.put_dashboard(
        DashboardName='Bedrock-Limits-Tracker',
        DashboardBody=json.dumps({'widgets': widgets})
    )
    print(f"Dashboard updated: Bedrock-Limits-Tracker ({len(accounts)} accounts, {len(all_models)} models)")


# ============ Helper Functions ============

def get_model_usage(cw_client, profiles):
    """Get peak 1-min usage in last 5 minutes per model using batch API."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=5)
    usage = {}
    model_ids = list(set(p['model_id'] for p in profiles if p['model_id']))
    # Also collect inference profile IDs (CW records metrics under the ID used for invocation)
    profile_id_to_model = {}
    for p in profiles:
        if p['profile_id'] and p['model_id'] and p['profile_id'] != p['model_id']:
            profile_id_to_model[p['profile_id']] = p['model_id']
    # Query both base model IDs and profile IDs
    all_query_ids = model_ids + list(profile_id_to_model.keys())

    if not all_query_ids:
        return usage

    # Build batch queries
    queries = []
    for i, qid in enumerate(all_query_ids):
        queries.append({
            'Id': f'rpm{i}',
            'MetricStat': {
                'Metric': {'Namespace': 'AWS/Bedrock', 'MetricName': 'Invocations',
                           'Dimensions': [{'Name': 'ModelId', 'Value': qid}]},
                'Period': 60, 'Stat': 'Sum'
            }, 'ReturnData': True
        })
        queries.append({
            'Id': f'tin{i}',
            'MetricStat': {
                'Metric': {'Namespace': 'AWS/Bedrock', 'MetricName': 'InputTokenCount',
                           'Dimensions': [{'Name': 'ModelId', 'Value': qid}]},
                'Period': 60, 'Stat': 'Sum'
            }, 'ReturnData': True
        })
        queries.append({
            'Id': f'tout{i}',
            'MetricStat': {
                'Metric': {'Namespace': 'AWS/Bedrock', 'MetricName': 'OutputTokenCount',
                           'Dimensions': [{'Name': 'ModelId', 'Value': qid}]},
                'Period': 60, 'Stat': 'Sum'
            }, 'ReturnData': True
        })

    # Execute in batches of 500
    all_results = {}
    for batch_start in range(0, len(queries), 500):
        batch = queries[batch_start:batch_start + 500]
        try:
            resp = cw_client.get_metric_data(
                MetricDataQueries=batch,
                StartTime=start, EndTime=now
            )
            for r in resp.get('MetricDataResults', []):
                all_results[r['Id']] = r['Values']
        except Exception as e:
            print(f"  Batch metric query error: {e}")

    # Parse results - map profile IDs back to base model
    for i, qid in enumerate(all_query_ids):
        # Map to base model ID if this was a profile ID query
        base_model = profile_id_to_model.get(qid, qid)

        rpm_vals = all_results.get(f'rpm{i}', [])
        tin_vals = all_results.get(f'tin{i}', [])
        tout_vals = all_results.get(f'tout{i}', [])

        if rpm_vals:
            current = usage.get(base_model, {}).get('rpm', 0)
            usage.setdefault(base_model, {})['rpm'] = max(current, max(rpm_vals))
        tpm = (max(tin_vals) if tin_vals else 0) + (max(tout_vals) if tout_vals else 0)
        if tpm > 0:
            current = usage.get(base_model, {}).get('tpm', 0)
            usage.setdefault(base_model, {})['tpm'] = max(current, tpm)

    return usage

def get_cached_quotas(account_id, region, session):
    """Read quotas from S3 cache. Refresh from Service Quotas API if older than 24h."""
    s3 = boto3.client('s3')
    cache_bucket = os.environ.get('CACHE_BUCKET', '')
    cache_key = f'quota-cache/{account_id}/{region}.json'

    try:
        obj = s3.get_object(Bucket=cache_bucket, Key=cache_key)
        cached = json.loads(obj['Body'].read().decode())
        cached_at = datetime.fromisoformat(cached['_cached_at'])
        age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600

        if age_hours < 24:
            del cached['_cached_at']
            return cached
        else:
            print(f"  Quota cache expired for {account_id}/{region} ({age_hours:.0f}h old)")
    except s3.exceptions.NoSuchKey:
        print(f"  No quota cache for {account_id}/{region}, fetching fresh")
    except Exception as e:
        print(f"  Cache read error: {e}, fetching fresh")

    # Fetch from Service Quotas
    sq = session.client('service-quotas')
    model_quotas = extract_model_quotas(sq)

    # Store in cache
    try:
        cache_data = dict(model_quotas)
        cache_data['_cached_at'] = datetime.now(timezone.utc).isoformat()
        s3.put_object(
            Bucket=cache_bucket, Key=cache_key,
            Body=json.dumps(cache_data, default=str).encode(),
            ContentType='application/json'
        )
        print(f"  Cached quotas for {account_id}/{region}")
    except Exception as e:
        print(f"  Cache write error: {e}")

    return model_quotas


def extract_model_quotas(sq_client):
    """Extract per-model TPM and RPM quotas from Service Quotas."""
    model_quotas = {}
    tpm_pattern = re.compile(r'(?:tokens per minute|TPM).*?for (.+)', re.IGNORECASE)
    rpm_pattern = re.compile(r'(?:requests per minute|RPM).*?for (.+)', re.IGNORECASE)
    cross_region_pattern = re.compile(r'cross.?region', re.IGNORECASE)

    for list_fn in ['list_service_quotas', 'list_aws_default_service_quotas']:
        try:
            paginator = sq_client.get_paginator(list_fn)
            kwargs = {'ServiceCode': 'bedrock'}
            for page in paginator.paginate(**kwargs):
                for q in page.get('Quotas', []):
                    name = q['QuotaName']
                    value = q['Value']
                    is_cross_region = bool(cross_region_pattern.search(name))

                    tpm_match = tpm_pattern.search(name)
                    rpm_match = rpm_pattern.search(name)

                    if tpm_match:
                        model_name = tpm_match.group(1).strip()
                        key = normalize_model_name(model_name)
                        if key not in model_quotas:
                            model_quotas[key] = {'model_name': model_name}
                        field = 'cross_region_tpm' if is_cross_region else 'single_region_tpm'
                        if field not in model_quotas[key]:
                            model_quotas[key][field] = value

                    elif rpm_match:
                        model_name = rpm_match.group(1).strip()
                        key = normalize_model_name(model_name)
                        if key not in model_quotas:
                            model_quotas[key] = {'model_name': model_name}
                        field = 'cross_region_rpm' if is_cross_region else 'single_region_rpm'
                        if field not in model_quotas[key]:
                            model_quotas[key][field] = value

        except Exception as e:
            print(f"  Error in {list_fn}: {e}")

    return model_quotas


def normalize_model_name(name):
    return re.sub(r'[^a-z0-9]', '', name.lower())


def list_inference_profiles(bedrock_client):
    """List all inference profiles (SYSTEM_DEFINED + APPLICATION)."""
    profiles = []
    provisioned_arns = set()
    try:
        resp = bedrock_client.list_provisioned_model_throughputs()
        for pt in resp.get('provisionedModelSummaries', []):
            provisioned_arns.add(pt.get('provisionedModelArn', ''))
    except Exception:
        pass

    try:
        paginator = bedrock_client.get_paginator('list_inference_profiles')
        for profile_type_filter in ['SYSTEM_DEFINED', 'APPLICATION']:
            try:
                for page in paginator.paginate(typeEquals=profile_type_filter):
                    for p in page.get('inferenceProfileSummaries', []):
                        profile_type = p.get('type', 'APPLICATION')
                        model_arn = ''
                        model_id = ''
                        if p.get('models'):
                            model_arn = p['models'][0].get('modelArn', '')
                            model_id = model_arn.split('/')[-1]

                        is_cross_region = profile_type == 'SYSTEM_DEFINED'
                        is_provisioned = model_arn in provisioned_arns

                        if is_provisioned:
                            capacity = 'Provisioned'
                        elif is_cross_region:
                            capacity = 'Cross-Region'
                        else:
                            capacity = 'On-Demand'

                        profiles.append({
                            'arn': p.get('inferenceProfileArn', ''),
                            'name': p.get('inferenceProfileName', ''),
                            'profile_id': p.get('inferenceProfileId', ''),
                            'display_id': p.get('inferenceProfileId', ''),
                            'model_id': model_id,
                            'type': profile_type,
                            'is_cross_region': is_cross_region,
                            'capacity': capacity,
                            'status': p.get('status', 'ACTIVE')
                        })
            except Exception as e:
                print(f"  Error listing {profile_type_filter}: {e}")
    except Exception as e:
        print(f"  Could not list inference profiles: {e}")
    return profiles
