# Bedrock Limits Tracker

Centrally monitors **per-model** Amazon Bedrock quotas (TPM/RPM) across multiple AWS accounts and regions using CloudWatch Cross-Account Observability. Provides **RAG (Red/Amber/Green)** status per inference profile.

## Key Features

- **Per-model tracking**: Tokens-per-minute (TPM) and Requests-per-minute (RPM) quotas tracked individually for each model
- **Per-account/region visibility**: See which accounts and regions are approaching limits
- **RAG status**: 🔴 RED (>90%) | 🟠 AMBER (>70%) | 🟢 GREEN (≤70%)
- **Cross-region double quota**: Inference profiles using cross-region routing automatically get **2x** the quota applied
- **Centralized alerts**: All metrics flow to a single monitoring account via OAM

## Architecture

```
Source Accounts (per region)              Monitoring Account
┌────────────────────────────┐           ┌──────────────────────────────────────┐
│ IAM Role (assumable)       │           │ OAM Sink (receives native CW metrics)│
│   - service-quotas:List*   │           │                                      │
│   - bedrock:List*          │           │ EventBridge (rate: N min)            │
│   - cloudwatch:GetMetric*  │           │   → bedrock_LimitsTracker Lambda     │
│                            │◄─assumes──│     For each account × region:       │
│ OAM Link ──metrics──────► │───────────│       1. Assume spoke role            │
│   (shares AWS/Bedrock      │           │       2. List Service Quotas (bedrock)│
│    native metrics)         │           │       3. List inference profiles      │
│                            │           │       4. Get CW usage (per model)     │
└────────────────────────────┘           │       5. Compute utilization %        │
                                         │       6. Apply 2x for cross-region    │
                                         │       7. Assign RAG status            │
                                         │       8. Publish metrics              │
                                         │                                      │
                                         │ CloudWatch Dashboard                 │
                                         │   🚦 Account/Region RAG heatmap      │
                                         │   📊 TPM utilization per model       │
                                         │   📊 RPM utilization per model       │
                                         │   🌐 Cross-region profile count      │
                                         │                                      │
                                         │ Alarms → SNS → Email                 │
                                         │   🔴 RED alarm (>90%)                │
                                         │   🟠 AMBER alarm (>70%)              │
                                         │   📊 TPM/RPM threshold alarms        │
                                         └──────────────────────────────────────┘
```

## RAG Logic

| Status | Condition | Meaning |
|--------|-----------|---------|
| 🔴 RED | Utilization > 90% | Critical - throttling imminent, request quota increase |
| 🟠 AMBER | Utilization > 70% | Warning - approaching limits, plan ahead |
| 🟢 GREEN | Utilization ≤ 70% | Healthy - sufficient headroom |

**Cross-region inference profiles** (type=SYSTEM) get **double** the effective quota because they route across multiple regions. The utilization calculation uses:
- Single-region profile: `actual_usage / single_region_quota × 100`
- Cross-region profile: `actual_usage / (cross_region_quota × 2) × 100`

## Roles & Trust Architecture

> 📐 Open `deploy-time-flow.svg` and `runtime-flow.svg` in a browser for visual diagrams.

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              DEPLOY-TIME FLOW                                       │
│                                                                                     │
│  Admin Account                           Spoke Account                              │
│  ┌─────────────────────────────┐         ┌─────────────────────────────────────┐   │
│  │ AWSCloudFormation            │   ①     │ AWSCloudFormation                    │   │
│  │ StackSetAdministrationRole   │────────►│ StackSetExecutionRole                │   │
│  │                              │ assumes │                                      │   │
│  │ Created by:                  │         │ Created by:                          │   │
│  │   stackset-admin-role.yaml   │         │   stackset-execution-role.yaml       │   │
│  │                              │         │                                      │   │
│  │ Trusted by:                  │         │ Trusted by:                          │   │
│  │   cloudformation.amazonaws.  │         │   AdminAccount's AdminRole           │   │
│  │   com                        │         │                                      │   │
│  │                              │         │ Allows:                              │   │
│  │ Allows:                      │         │   • iam:CreateRole (SpokeRole only)  │   │
│  │   • sts:AssumeRole →        │         │   • oam:CreateLink/DeleteLink        │   │
│  │     *:ExecutionRole          │         │   • cloudwatch:Link                  │   │
│  └─────────────────────────────┘         │   • cloudformation:*                 │   │
│                                           └──────────────┬──────────────────────┘   │
│                                                          │ ② iam:CreateRole          │
│                                                          ▼                           │
│                                           ┌─────────────────────────────────────┐   │
│                                           │ BedrockLimitsTrackerSpokeRole        │   │
│                                           │ + OAM Link                           │   │
│                                           │                                      │   │
│                                           │ Created by: source-account.yaml      │   │
│                                           │             (deployed via StackSet)   │   │
│                                           └─────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              RUNTIME FLOW                                            │
│                                                                                     │
│  Monitoring Account                      Spoke Account                              │
│  ┌─────────────────────────────┐         ┌─────────────────────────────────────┐   │
│  │ BedrockLimitsTracker         │   ③     │ BedrockLimitsTrackerSpokeRole        │   │
│  │ LambdaRole                   │────────►│                                      │   │
│  │                              │ assumes │ Trusted by:                          │   │
│  │ Created by:                  │         │   MonitoringAccount's LambdaRole     │   │
│  │   monitoring-account.yaml    │         │                                      │   │
│  │                              │         │ Allows:                              │   │
│  │ Trusted by:                  │         │   • service-quotas:List*/Get*        │   │
│  │   lambda.amazonaws.com       │         │   • bedrock:ListFoundationModels     │   │
│  │                              │         │   • bedrock:ListInferenceProfiles    │   │
│  │ Allows:                      │         │   • cloudwatch:GetMetricData         │   │
│  │   • sts:AssumeRole →        │         └─────────────────────────────────────┘   │
│  │     *:SpokeRole              │                                                   │
│  │   • cloudwatch:PutMetricData │         ┌─────────────────────────────────────┐   │
│  │   • cloudwatch:PutDashboard  │   ④     │ OAM Link                             │   │
│  │   • ssm:GetParameter         │◄────────│   → shares AWS/Bedrock metrics       │   │
│  └─────────────────────────────┘ metrics  │   → to Monitoring Account's Sink     │   │
│                                   (free)  └─────────────────────────────────────┘   │
│  ┌─────────────────────────────┐                                                    │
│  │ OAM Sink                     │                                                    │
│  │   Receives metrics from      │                                                    │
│  │   all linked spoke accounts  │                                                    │
│  └─────────────────────────────┘                                                    │
└─────────────────────────────────────────────────────────────────────────────────────┘

LEGEND:
  ① StackSet assumes ExecutionRole in spoke (deploy-time only)
  ② ExecutionRole creates SpokeRole + OAM Link (deploy-time only)
  ③ Lambda assumes SpokeRole to read quotas (runtime, every N minutes)
  ④ OAM Link sends native AWS/Bedrock metrics to Sink (continuous, free)
```

### Role Summary Table

| Role | Where Created | Template | Trusted By | Permissions |
|------|--------------|----------|------------|-------------|
| `AWSCloudFormationStackSetAdministrationRole` | Admin account | `stackset-admin-role.yaml` | `cloudformation.amazonaws.com` | `sts:AssumeRole` → ExecutionRole in any account |
| `AWSCloudFormationStackSetExecutionRole` | Each spoke account | `stackset-execution-role.yaml` | Admin account's AdminRole | `iam:CreateRole` (scoped), `oam:*Link`, `cloudwatch:Link`, `cloudformation:*` |
| `BedrockLimitsTrackerLambdaRole` | Monitoring account | `monitoring-account.yaml` | `lambda.amazonaws.com` | `sts:AssumeRole` → SpokeRole, `cloudwatch:PutMetricData/PutDashboard`, `ssm:Get/PutParameter`, `s3:GetObject/PutObject` (cache bucket) |
| `BedrockLimitsTrackerSpokeRole` | Each spoke account | `source-account.yaml` (via StackSet) | Monitoring account's LambdaRole | `servicequotas:List*/Get*`, `bedrock:List*` (profiles, models, provisioned), `cloudwatch:GetMetric*` |

## How It Works — Detailed Operational Flow

### What Runs, When, and What Data It Reads

```
┌─────────────────────────────────────────────────────────────────────────┐
│ EVERY 1 MINUTE (Lambda execution ~18s)                                  │
│                                                                         │
│ 1. Read account list from SSM Parameter                                 │
│ 2. For each account × region (in parallel, 10 threads):                 │
│    a. Assume BedrockLimitsTrackerSpokeRole                              │
│    b. Read quota cache from S3 (if <24h old, skip Service Quotas API)   │
│    c. List inference profiles (SYSTEM_DEFINED + APPLICATION)            │
│    d. Batch query CloudWatch get_metric_data:                           │
│       - AWS/Bedrock Invocations (per ModelId) → RPM                     │
│       - AWS/Bedrock InputTokenCount (per ModelId) → TPM                 │
│       - AWS/Bedrock OutputTokenCount (per ModelId) → TPM                │
│       - Queries BOTH base model IDs and inference profile IDs           │
│       - Returns peak 1-min value from last 5 minutes                    │
│    e. Compute utilization: usage / quota × 100                          │
│    f. Publish QuotaLimit_TPM and QuotaLimit_RPM metrics                 │
│ 3. Publish summary metrics (TotalProfiles, Over50/75 counts)            │
│ 4. Rebuild dashboard (markdown table + widget definitions)              │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│ EVERY 24 HOURS (automatic, triggered by cache expiry)                   │
│                                                                         │
│ When S3 cache is >24h old:                                              │
│ - Calls Service Quotas API (ListServiceQuotas + ListAWSDefaultQuotas)   │
│ - Stores result in S3: quota-cache/{account}/{region}.json              │
│ - This adds ~200s to that one execution (rate-limited API)              │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│ CONTINUOUS (no Lambda needed)                                           │
│                                                                         │
│ - Metrics Insights charts query AWS/Bedrock namespace directly          │
│ - Updates every 60 seconds in the dashboard automatically              │
│ - Shows top 10 models by actual invocation volume                       │
│ - OAM Link streams native metrics from spoke accounts (free)            │
└─────────────────────────────────────────────────────────────────────────┘
```

### Data Flow Diagram

```
Spoke Account (Bedrock usage)
  │
  ├── AWS/Bedrock metrics (automatic, every minute)
  │     └── Invocations, InputTokenCount, OutputTokenCount per ModelId
  │
  ├── OAM Link ──────────────────────────────────────────┐
  │     └── Shares native metrics to monitoring account   │
  │                                                       ▼
  └── BedrockLimitsTrackerSpokeRole ◄── Lambda assumes ── Monitoring Account
        │                                                    │
        ├── Service Quotas API (daily)                       ├── S3 cache (quotas)
        ├── Bedrock ListInferenceProfiles                    ├── CloudWatch PutMetricData
        └── CloudWatch GetMetricData (batch)                 ├── CloudWatch PutDashboard
                                                             └── Dashboard (auto-refresh)
```

## Cost Analysis

### Lambda Execution Cost (us-east-1 pricing)

| Schedule | Invocations/month | Duration | GB-seconds | Lambda Cost | CW Metrics Cost | Total |
|----------|------------------:|---------:|-----------:|------------:|----------------:|------:|
| **Every 1 min** | 43,200 | 18s × 256MB | 198,000 | $3.30 | ~$60 (196 metrics) | **~$63/month** |
| **Every 5 min** | 8,640 | 18s × 256MB | 39,600 | $0.66 | ~$60 (196 metrics) | **~$61/month** |
| **Every 15 min** | 2,880 | 18s × 256MB | 13,200 | $0.22 | ~$60 (196 metrics) | **~$60/month** |
| **Every 60 min** | 720 | 18s × 256MB | 3,300 | $0.06 | ~$60 (196 metrics) | **~$60/month** |

> **Note:** The dominant cost is custom CloudWatch metrics ($0.30/metric/month × 196 models = ~$60). Lambda execution cost is negligible in comparison. The Metrics Insights charts are free (included in CloudWatch).

### Cost Breakdown

| Component | Monthly Cost | Notes |
|-----------|------------:|-------|
| Custom metrics (QuotaLimit_TPM/RPM) | ~$60 | 196 unique model metrics × $0.30 |
| Summary metrics (Over50/75, Total) | $1.50 | 5 metrics × $0.30 |
| Lambda execution (1-min schedule) | $3.30 | 43,200 invocations × 18s × 256MB |
| S3 cache storage | $0.01 | 6 JSON files, <1MB total |
| S3 requests | $0.02 | ~86,400 GET + 6 PUT/day |
| CloudWatch Dashboard | $3.00 | 1 custom dashboard |
| OAM (metrics sharing) | **$0** | Free for metrics |
| Native AWS/Bedrock metrics | **$0** | Vended, always on |
| Metrics Insights queries | **$0** | Included in dashboard |
| **Total** | **~$68/month** | For 3 accounts × 2 regions |

### Cost at Scale

| Scale | Models | Custom Metrics Cost | Lambda Cost (1-min) | Total |
|-------|-------:|--------------------:|--------------------:|------:|
| 3 accounts × 2 regions | ~196 | $60 | $3 | ~$68 |
| 10 accounts × 3 regions | ~500 | $150 | $5 | ~$160 |
| 50 accounts × 3 regions | ~2,000 | $400* | $10 | ~$415 |
| 100 accounts × 5 regions | ~5,000 | $750* | $20 | ~$775 |

*Volume discounts apply after 10,000 metrics ($0.10/metric for 10K-240K tier)

### Cost Optimization Tips

- **Reduce custom metrics**: Only publish QuotaLimit metrics for models that have been invoked (skip unused models) — can reduce by 80%+
- **Use 5-min schedule**: Saves $2.64/month on Lambda (negligible) but real-time graphs still work every minute
- **The real-time Metrics Insights charts are free** regardless of schedule — they query native metrics directly

## Data Freshness & How Monitoring Works

The dashboard uses **two data sources** with different refresh behaviors:

| Component | Data Source | Refresh | What it shows |
|-----------|------------|---------|---------------|
| **Real-time graphs** (RPM/TPM Utilization %) | CloudWatch metric math | **Every minute** (native) | Live spike as it happens — drops to 0 when traffic stops |
| **Per-account tables** (Status, Used, %) | Lambda reads CloudWatch | **Every 2 minutes** | Peak usage in last 5 minutes — persists briefly after spike ends |
| **Number widgets** (>50%, >75%) | Lambda publishes metrics | **Every 2 minutes** | Count of models exceeding thresholds (5-min peak) |
| **Regions Affected** | Lambda computes | **Every 2 minutes** | Regions with models over threshold (5-min peak) |
| **Quota limits** (TPM Limit, RPM Limit) | S3 cache from Service Quotas | **Every 24 hours** | Actual account-specific quota values |

**Why the table can show usage when the graph shows 0:**

The table uses the **peak value from the last 5 minutes** (`max(datapoints)`). This means:
- A spike at 23:50 will show in the table until ~23:55 (even if traffic stopped at 23:51)
- The real-time graph drops to 0 immediately when traffic stops
- This is intentional — gives you a window to notice recent spikes without staring at the dashboard

**Why the graph can show a spike the table doesn't reflect:**

The Lambda runs every 2 minutes. If a spike starts between Lambda runs, the graph shows it immediately but the table won't update until the next Lambda execution (up to 2 minutes later).

**Timeline example:**
```
23:50:00  Traffic spike starts → Graph shows spike immediately
23:50:30  Lambda runs → Table shows spike, numbers update
23:52:00  Traffic stops → Graph drops to 0
23:52:30  Lambda runs → Table still shows spike (5-min peak window)
23:55:00  5-min window expires → Table shows 0 on next Lambda run
```

## Quota Caching

Service Quotas API has a low rate limit (~5 req/s) and Bedrock has hundreds of quotas. To avoid `TooManyRequestsException`, quotas are **cached in S3** and refreshed once per day.

**How it works:**
- First invocation: fetches quotas from Service Quotas API, stores in `s3://bedrock-limits-cache-{AccountId}/quota-cache/{account}/{region}.json`
- Subsequent invocations (within 24h): reads from S3 cache (~70s vs ~240s)
- After 24h: automatically refreshes the cache

**To force a quota refresh** (e.g., after requesting a quota increase):

```bash
# Delete the cache for a specific account/region
AWS_PROFILE=<YOUR_PROFILE> aws s3 rm \
  s3://bedrock-limits-cache-<MONITORING_ACCOUNT_ID>/quota-cache/<SOURCE_ACCOUNT_ID>/us-east-1.json

# Next Lambda invocation will fetch fresh quotas for that account/region
AWS_PROFILE=<YOUR_PROFILE> aws lambda invoke \
  --function-name bedrock_LimitsTracker \
  --region us-east-1 --cli-read-timeout 600 /tmp/out.json
```

**To refresh ALL quotas:**

```bash
AWS_PROFILE=<YOUR_PROFILE> aws s3 rm \
  s3://bedrock-limits-cache-<MONITORING_ACCOUNT_ID>/quota-cache/ --recursive

# Next invocation fetches everything fresh (will take ~240s)
```

## Pricing Considerations

This solution is designed to be cost-effective by leveraging free-tier features wherever possible.

| Component | Cost | Notes |
|-----------|------|-------|
| **OAM metrics sharing** | **$0** | Cross-account observability for metrics and logs is free |
| **Native AWS/Bedrock metrics** | **$0** | Vended metrics — always published, no config needed |
| **Custom metrics (BedrockLimits namespace)** | ~$0.30/metric/month | ~8 metrics per profile × accounts × regions |
| **Lambda execution** | ~$0.01/month | 5-min runtime, 256MB, hourly |
| **CloudWatch Dashboards** | $3/month each | 2 dashboards = $6/month (first 3 free) |
| **CloudWatch Alarms** | $0.10/alarm/month | 4 alarms = $0.40/month (first 10 free) |
| **SSM Parameter** | Free | Standard tier |
| **S3 cache bucket** | ~$0.01/month | 6 JSON files, negligible storage + requests |

**Example: 5 accounts × 3 regions × 10 models = 150 profiles**
- Custom metrics: ~1,200 metrics × $0.30 = ~$360/month (volume discounts apply after 10K)
- Everything else: ~$7/month
- **Total: ~$367/month** for full cross-account per-model quota visibility

**Cost optimization tips:**
- Reduce `ExecutionFrequencyMinutes` to publish fewer datapoints (doesn't reduce metric count)
- Track only models actually in use (the Lambda already does this — only profiles with active models)
- Use metric math in dashboards instead of publishing derived metrics

**What you DON'T pay for:**
- OAM link/sink (free for metrics)
- Native Bedrock metrics flowing to monitoring account (free)
- Bedrock metrics in source accounts (vended, always free)

## Dashboard

A single dashboard `Bedrock-Limits-Tracker` provides full visibility:

```
┌─────────────────────────────────────────────────────────┐
│ # Bedrock Limits Tracker                                │
│ 🔴 RED: 0 | 🟠 AMBER: 0 | 🟢 GREEN: 3 | 🔵 Not Used: 369 │
├─────────────────────────────────────────────────────────┤
│ 🚦 Account/Region Worst Status Over Time (line chart)   │
├────────────────────────┬──────────┬──────────┬──────────┤
│ 🔥 Top 10 Highest     │  Total   │ ⚠️ Over  │ 🔴 Over  │
│    Utilization         │ Profiles │   50%    │   75%    │
│                        │   372    │    0     │    0     │
│ # | Status | Model    ├──────────┴──────────┴──────────┤
│ 1 | 🟠 | claude-4.6   │ ⚠️ Profiles Near Limits        │
│ 2 | 🟢 | nova-lite    │ Account      | >75% | >50%     │
│ ...                    │ <ACCOUNT_A>  |    2 |    5     │
│                        │ <ACCOUNT_B>  |    0 |    1     │
│                        │ <ACCOUNT_C>  |    1 |    3     │
├────────────────────────┴────────────────────────────────┤
│ ### Account: <ACCOUNT_A>                               │
│ | Status | Account | Region | Model | Profile ID |     │
│ |   Type | Capacity | TPM Limit | TPM Used | RPM Limit │
│ |   RPM Used | Utilization % |                         │
│ (sorted by utilization descending)                      │
├─────────────────────────────────────────────────────────┤
│ ### Account: <ACCOUNT_B>                               │
├─────────────────────────────────────────────────────────┤
│ ### Account: <ACCOUNT_C>                               │
└─────────────────────────────────────────────────────────┘
```

### Widget Types Used

| Widget | Type | Purpose |
|--------|------|---------|
| Summary | Text (markdown) | RAG counts + last updated timestamp |
| Worst Status Over Time | Metric (timeSeries) | Historical trend of account/region status |
| Total Profiles | Metric (singleValue) | Big number — total inference profiles |
| Over 50% | Metric (singleValue) | Big number — profiles exceeding 50% utilization |
| Over 75% | Metric (singleValue) | Big number — profiles exceeding 75% utilization |
| Top 10 Highest | Text (markdown table) | Quick view of hottest profiles |
| Profiles Near Limits | Text (markdown table) | Per-account breakdown of threshold breaches |
| Per-Account Tables | Text (markdown table) | Full detail per account, sorted by utilization |

### Dashboard Columns (per-account tables)

| Column | Description |
|--------|-------------|
| Status | 🔴 RED (>90%) 🟠 AMBER (>70%) 🟢 GREEN (active, ≤70%) 🔵 NOT_USED |
| Account | Full 12-digit AWS account ID |
| Region | AWS region (e.g., us-east-1) |
| Model | Bedrock model ID (e.g., anthropic.claude-sonnet-4-6) |
| Profile ID | Inference profile ID — short hash for Application (e.g., `prrn3lhzhjr8`), model path for System |
| Type | `System` (AWS-managed cross-region) or `Application` (custom-created) |
| Capacity | `On-Demand`, `Cross-Region` (2x quota), or `Provisioned` |
| TPM Limit | Actual tokens-per-minute quota (from Service Quotas API, account-specific) |
| TPM Used | Current tokens-per-minute usage (from CloudWatch AWS/Bedrock metrics) |
| RPM Limit | Actual requests-per-minute quota |
| RPM Used | Current requests-per-minute usage |
| Utilization % | max(TPM%, RPM%) — determines RAG status |

### Key Behaviors

- **Sorted by utilization descending** — highest usage profiles always at top
- **Per-account sections** — each account has its own scrollable table with account ID in every row
- **Cross-region 2x multiplier** — System profiles show doubled effective quota
- **Both profile types** — System (SYSTEM_DEFINED) and Application profiles listed separately
- **Real quotas** — reads actual account-specific limits, not documentation defaults
- **Number widgets** — big single-value displays for at-a-glance threshold monitoring
- **Dashboard auto-updates** — Lambda refreshes the dashboard on every execution

| Metric | Dimensions | Description |
|--------|-----------|-------------|
| `TokensPerMinuteUtilization` | AccountId, Region, ModelId, ProfileArn | TPM usage as % of effective quota |
| `RequestsPerMinuteUtilization` | AccountId, Region, ModelId, ProfileArn | RPM usage as % of effective quota |
| `RAGStatus` | AccountId, Region, ModelId, ProfileArn | 0=GREEN, 1=AMBER, 2=RED |
| `EffectiveTPMQuota` | AccountId, Region, ModelId, ProfileArn | Effective TPM limit (2x if cross-region) |
| `EffectiveRPMQuota` | AccountId, Region, ModelId, ProfileArn | Effective RPM limit (2x if cross-region) |
| `IsCrossRegionProfile` | AccountId, Region, ModelId, ProfileArn | 1 if cross-region, 0 if single |
| `AccountRegionRAGStatus` | AccountId, Region | Worst RAG status across all models |
| `ActiveInferenceProfiles` | AccountId, Region | Count of active profiles |

## Deployment

### Where Can This Be Deployed?

This solution does **NOT** require the payer/management account. Here's why:

- We're using **SELF_MANAGED** StackSets (not SERVICE_MANAGED) — no Organizations API needed
- The OAM Sink just needs a policy listing account IDs — no org dependency
- The Lambda assumes roles via explicit account IDs from SSM — no org trust needed

The monitoring account can be **any account** — payer, linked, or standalone. The only requirement is that spoke accounts trust it (via the IAM roles deployed by `source-account.yaml`).

| Scenario | Works? | Notes |
|----------|--------|-------|
| Monitoring on payer account | ✅ | Common choice, central billing visibility |
| Monitoring on a dedicated observability account | ✅ | Best practice for large orgs |
| Monitoring on any linked account | ✅ | Just needs spoke roles to trust it |
| No AWS Organizations at all | ✅ | Explicit account IDs in sink policy + SSM |

### Prerequisites: StackSet Roles (for brand-new environments)

If you don't have StackSet roles already provisioned (or are using `SELF_MANAGED` mode), deploy these first:

**Step 0a: Deploy admin role (in the account that will manage the StackSet)**

```bash
aws cloudformation deploy \
  --template-file stackset-admin-role.yaml \
  --stack-name stackset-admin-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

**Step 0b: Deploy execution role (in EACH target/spoke account)**

```bash
# Run this in each spoke account (or use a script to iterate)
ADMIN_ACCOUNT_ID="999999999999"  # Account where you'll create the StackSet

aws cloudformation deploy \
  --template-file stackset-execution-role.yaml \
  --stack-name stackset-execution-role \
  --parameter-overrides AdminAccountId=$ADMIN_ACCOUNT_ID \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

> **Note:** If using `SERVICE_MANAGED` permission model (Organizations), skip Step 0 entirely — AWS handles roles automatically.

### Step 1: Deploy Monitoring Account Stack

> **⚠️ Note:** When using `--parameter-overrides`, quote each `Key=Value` pair individually. Values containing commas (like account lists or region lists) will be misinterpreted otherwise.

```bash
aws cloudformation deploy \
  --template-file monitoring-account.yaml \
  --stack-name bedrock-limits-tracker \
  --parameter-overrides \
    "SourceAccountIds=111111111111,222222222222" \
    "BedrockRegions=us-east-1,us-west-2,eu-west-1" \
    "ExecutionFrequencyMinutes=60" \
    "NotificationEmail=team@example.com" \
    "AlarmThresholdPercent=80" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

### Step 2: Get the Sink ARN

```bash
SINK_ARN=$(aws cloudformation describe-stacks \
  --stack-name bedrock-limits-tracker \
  --query 'Stacks[0].Outputs[?OutputKey==`SinkArn`].OutputValue' \
  --output text --region us-east-1)
echo $SINK_ARN
```

### Step 3: Deploy Source Account Stack (each spoke)

```bash
aws cloudformation deploy \
  --template-file source-account.yaml \
  --stack-name bedrock-limits-tracker-spoke \
  --parameter-overrides \
    SinkArn="$SINK_ARN" \
    MonitoringAccountId="999999999999" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

**For multiple accounts via StackSets:**

```bash
# Option A: SELF_MANAGED (uses roles from Step 0)
aws cloudformation create-stack-set \
  --stack-set-name bedrock-limits-tracker-spokes \
  --template-body file://source-account.yaml \
  --parameters \
    ParameterKey=SinkArn,ParameterValue="$SINK_ARN" \
    ParameterKey=MonitoringAccountId,ParameterValue="999999999999" \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SELF_MANAGED \
  --administration-role-arn "arn:aws:iam::999999999999:role/AWSCloudFormationStackSetAdministrationRole" \
  --execution-role-name AWSCloudFormationStackSetExecutionRole

aws cloudformation create-stack-instances \
  --stack-set-name bedrock-limits-tracker-spokes \
  --accounts "111111111111" "222222222222" \
  --regions us-east-1 us-west-2 eu-west-1

# Option B: SERVICE_MANAGED (Organizations — no pre-provisioned roles needed)
aws cloudformation create-stack-set \
  --stack-set-name bedrock-limits-tracker-spokes \
  --template-body file://source-account.yaml \
  --parameters \
    ParameterKey=SinkArn,ParameterValue="$SINK_ARN" \
    ParameterKey=MonitoringAccountId,ParameterValue="999999999999" \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

aws cloudformation create-stack-instances \
  --stack-set-name bedrock-limits-tracker-spokes \
  --deployment-targets OrganizationalUnitIds=ou-xxxx-xxxxxxxx \
  --regions us-east-1 us-west-2 eu-west-1
```

### Step 4: Deploy Lambda Code

```bash
cd lambda
zip index.zip index.py
aws lambda update-function-code \
  --function-name bedrock_LimitsTracker \
  --zip-file fileb://index.zip \
  --region us-east-1
```

### Step 5: Test

```bash
aws lambda invoke --function-name bedrock_LimitsTracker \
  --region us-east-1 /tmp/out.json && cat /tmp/out.json
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `OrganizationId` | — | Org ID to auto-allow all accounts to link |
| `SourceAccountIds` | — | Comma-separated account IDs to monitor |
| `BedrockRegions` | us-east-1,us-west-2,eu-west-1,ap-northeast-1 | Regions to scan |
| `ExecutionFrequencyMinutes` | 60 | Collection interval (5-1440) |
| `NotificationEmail` | — | Email for alarm notifications |
| `AlarmThresholdPercent` | 80 | Threshold for TPM/RPM alarms |

## File Structure

```
├── monitoring-account.yaml       # Hub: OAM Sink, Lambda, RAG Alarms, Dashboards
├── source-account.yaml           # Spoke: OAM Link, IAM Role (deployed per account)
├── stackset-admin-role.yaml      # StackSet admin role (deploy in admin account)
├── stackset-execution-role.yaml  # StackSet execution role (deploy in each spoke)
├── lambda/
│   └── index.py                  # Per-model quota collector + RAG publisher
├── deploy-time-flow.svg          # Visual: StackSet role chain & resource creation
├── runtime-flow.svg              # Visual: Lambda quota collection & OAM metrics
└── README.md
```

## Specifying Accounts to Monitor

Accounts are configured in **two places** that must stay in sync:

| Where | What it controls | How to set |
|-------|-----------------|------------|
| `SourceAccountIds` parameter (monitoring-account.yaml) | OAM Sink policy (who can link) + initial SSM value | At deploy time |
| SSM Parameter `/BedrockLimitsTracker/SourceAccounts` | Which accounts the Lambda scans at runtime | Update anytime |

**Option A: Explicit account list** (recommended for controlled environments)

```bash
# At deploy time
--parameter-overrides SourceAccountIds="111111111111,222222222222,333333333333"
```

**Option B: Organization-wide** (for large orgs)

```bash
# Pass OrganizationId — sink allows ANY org account to link
--parameter-overrides OrganizationId=o-xxxxxxxxxx SourceAccountIds="111111111111,222222222222"
```

With Option B, the OAM Sink accepts links from all org accounts automatically, but the Lambda still only scans accounts listed in the SSM parameter. This lets you control who gets actively monitored vs who *can* link.

**After deployment**, add/remove accounts without redeploying:

```bash
aws ssm put-parameter \
  --name /BedrockLimitsTracker/SourceAccounts \
  --value "111111111111,222222222222,NEW_ACCOUNT_ID" \
  --type StringList --overwrite --region us-east-1
```

> **Important:** If using explicit `SourceAccountIds` (not org-wide), new accounts also need to be added to the sink policy — redeploy the monitoring stack with the updated list.

> **Monitoring account as a source:** If the monitoring account itself runs Bedrock workloads, include its account ID in `SourceAccountIds` and deploy `source-account.yaml` there too (the spoke role is needed for the Lambda to read its quotas). OAM Link is not needed — the monitoring account already sees its own metrics. If the monitoring account is purely for observability with no Bedrock usage, don't include it.

## Adding a New Account

1. Deploy `source-account.yaml` in the new account
2. Update SSM parameter:
   ```bash
   aws ssm put-parameter \
     --name /BedrockLimitsTracker/SourceAccounts \
     --value "111111111111,222222222222,NEW_ACCOUNT" \
     --type StringList --overwrite --region us-east-1
   ```
3. Next Lambda execution picks it up automatically

## Tested Deployment (verified working)

The following commands were tested and confirmed working on 2026-05-22:

```bash
# === STEP 1: Deploy monitoring stack (hub account) ===
AWS_PROFILE=<YOUR_PROFILE> aws cloudformation deploy \
  --template-file monitoring-account.yaml \
  --stack-name bedrock-limits-tracker \
  --parameter-overrides \
    "SourceAccountIds=<MONITORING_ACCOUNT_ID>,<SOURCE_ACCOUNT_A>,<SOURCE_ACCOUNT_B>" \
    "BedrockRegions=us-east-1,us-west-2" \
    "ExecutionFrequencyMinutes=60" \
    "AlarmThresholdPercent=80" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

# === STEP 2: Get Sink ARN ===
SINK_ARN=$(AWS_PROFILE=<YOUR_PROFILE> aws cloudformation describe-stacks \
  --stack-name bedrock-limits-tracker \
  --query 'Stacks[0].Outputs[?OutputKey==`SinkArn`].OutputValue' \
  --output text --region us-east-1)
echo "Sink ARN: $SINK_ARN"

# === STEP 3: Deploy spoke to monitoring account itself (skip OAM Link) ===
AWS_PROFILE=<YOUR_PROFILE> aws cloudformation deploy \
  --template-file source-account.yaml \
  --stack-name bedrock-limits-tracker-spoke \
  --parameter-overrides \
    "SinkArn=$SINK_ARN" \
    "MonitoringAccountId=<MONITORING_ACCOUNT_ID>" \
    "IsMonitoringAccount=true" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

# === STEP 4: Deploy spoke to each source account ===
AWS_PROFILE=<SOURCE_A_PROFILE> aws cloudformation deploy \
  --template-file source-account.yaml \
  --stack-name bedrock-limits-tracker-spoke \
  --parameter-overrides \
    "SinkArn=$SINK_ARN" \
    "MonitoringAccountId=<MONITORING_ACCOUNT_ID>" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

AWS_PROFILE=<SOURCE_B_PROFILE> aws cloudformation deploy \
  --template-file source-account.yaml \
  --stack-name bedrock-limits-tracker-spoke \
  --parameter-overrides \
    "SinkArn=$SINK_ARN" \
    "MonitoringAccountId=<MONITORING_ACCOUNT_ID>" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

# === STEP 5: Deploy Lambda code ===
cd lambda
zip -j /tmp/bedrock-limits-index.zip index.py
AWS_PROFILE=<YOUR_PROFILE> aws lambda update-function-code \
  --function-name bedrock_LimitsTracker \
  --zip-file fileb:///tmp/bedrock-limits-index.zip \
  --region us-east-1

# === STEP 6: Test (use --cli-read-timeout since scan takes ~60s) ===
AWS_PROFILE=<YOUR_PROFILE> aws lambda invoke \
  --function-name bedrock_LimitsTracker \
  --region us-east-1 \
  --cli-read-timeout 300 \
  /tmp/out.json && cat /tmp/out.json | python3 -m json.tool
```

**Gotchas encountered during deployment:**
1. `--parameter-overrides` — must quote each `"Key=Value"` pair when values contain commas
2. OAM Link cannot point to a sink in the same account — use `IsMonitoringAccount=true` for the hub
3. IAM service prefix for Service Quotas is `servicequotas` (no hyphen), not `service-quotas`
4. Lambda invocation takes ~60s for 3 accounts × 2 regions — use `--cli-read-timeout 300`
5. `list-inference-profiles` API only returns SYSTEM_DEFINED by default — must query APPLICATION separately
6. CloudWatch text widget markdown requires real newlines (`\n`), not escaped `\\n` literals
7. Lambda code updates may require a config change to force a new container (cold start with new code)

## Scaling to 100+ Accounts

The Lambda processes accounts/regions **in parallel** using a thread pool (`MAX_THREADS=10`). Performance:

| Accounts × Regions | Sequential | Threaded (10 workers) | Fits 1-min schedule? |
|--------------------|-----------:|----------------------:|:--------------------:|
| 3 × 2 = 6 | ~19s | ~18s | ✅ |
| 20 × 2 = 40 | ~120s | ~12s | ✅ |
| 100 × 2 = 200 | ~600s | ~60s | ✅ |
| 100 × 5 = 500 | ~25min | ~150s | ❌ (use 2-min schedule) |

**To adjust parallelism**, change `MAX_THREADS` in `lambda/index.py`. Higher values = faster but more memory. Lambda memory may need increasing for 100+ accounts (512MB recommended).

**For 500+ account/region combinations**, consider:
- Increase Lambda memory to 512MB or 1024MB (more CPU = faster threads)
- Use 2-minute schedule instead of 1-minute
- Or split into multiple Lambdas (one per region) using Step Functions fan-out

## Load Testing

A `load_test.py` script is included to generate Bedrock traffic for testing the dashboard.

**Usage:**
```bash
python3 load_test.py --profile <AWS_PROFILE> --region <REGION> \
  --model <MODEL_ID> --rpm <TARGET_RPM> --duration <SECONDS> --prompt-tokens <TOKENS>
```

**Important:** Some models require the inference profile ID (not the base model ID):
- Direct model: `amazon.nova-pro-v1:0` ✅
- Inference profile: `us.anthropic.claude-haiku-4-5-20251001-v1:0` ✅
- Base model (may fail): `anthropic.claude-haiku-4-5-20251001-v1:0` ❌ (requires inference profile)

**Example tests:**

```bash
# Nova Pro: hit >75% of 10 RPM limit (8 RPM, ~32K TPM)
python3 load_test.py --profile <YOUR_PROFILE> --region us-east-1 \
  --model amazon.nova-pro-v1:0 --rpm 8 --duration 150 --prompt-tokens 4000

# Haiku 4.5: moderate load via cross-region inference profile
python3 load_test.py --profile <YOUR_PROFILE> --region us-east-1 \
  --model us.anthropic.claude-haiku-4-5-20251001-v1:0 --rpm 30 --duration 120 --prompt-tokens 2000

# Run both simultaneously (separate terminals or background)
python3 load_test.py --profile <YOUR_PROFILE> --region us-east-1 \
  --model amazon.nova-pro-v1:0 --rpm 8 --duration 150 --prompt-tokens 4000 &
python3 load_test.py --profile <YOUR_PROFILE> --region us-east-1 \
  --model us.anthropic.claude-haiku-4-5-20251001-v1:0 --rpm 30 --duration 150 --prompt-tokens 2000 &
wait
```

**After running tests:**
- Real-time graphs update within 1-2 minutes automatically
- Per-account table updates on next Lambda run (every 2 minutes)
- Number widgets (>50%/>75%) update on next Lambda run

**Finding model quotas to plan tests:**
- Check the per-account table in the dashboard for TPM Limit / RPM Limit columns
- To hit 50%: set `--rpm` to half the RPM Limit
- To hit 75%: set `--rpm` to 75% of the RPM Limit

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Lambda AccessDenied on assume role | Verify spoke role trusts `BedrockLimitsTrackerLambdaRole` |
| No metrics for a model | Model may not have been invoked recently (no CW data) |
| All profiles show GREEN/0% | Usage metrics need recent invocations to populate |
| OAM Link fails | Check sink policy allows the source account |
| OAM Link fails in monitoring account | Use `IsMonitoringAccount=true` — can't link to own sink |
| Cross-region quota not doubled | Verify profile type is `SYSTEM_DEFINED` (not `APPLICATION`) |
| `models_tracked: 0` | Spoke role missing `servicequotas:` permissions (note: no hyphen) |
| APPLICATION profiles not showing | API requires explicit `typeEquals=APPLICATION` query |
| Dashboard shows raw text not table | Markdown needs real newlines — redeploy Lambda |
| `TooManyRequestsException` on quotas | Service Quotas API rate limit — Lambda still succeeds with partial data |
| Lambda timeout (>300s) | Increase timeout in template or reduce `BedrockRegions` count |
| Profile ID shows random chars | That's correct for APPLICATION profiles (e.g., `prrn3lhzhjr8`) |
