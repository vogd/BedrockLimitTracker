"""
Bedrock Load Test - Generate TPM/RPM usage to test quota monitoring.
Sends parallel requests to hit ~50% of quota limits.

Usage:
  AWS_PROFILE=<YOUR_PROFILE> python3 load_test.py --model anthropic.claude-3-haiku-20240307-v1:0 --region us-west-2 --rpm 400 --duration 120
"""
import argparse
import boto3
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def invoke_model(client, model_id, prompt_tokens=500):
    """Send a single Converse request with ~prompt_tokens of input."""
    # Generate a prompt that uses roughly the target token count
    padding = "Explain quantum computing. " * (prompt_tokens // 5)
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{
                'role': 'user',
                'content': [{'text': f"Respond in exactly 10 words. {padding}"}]
            }],
            inferenceConfig={'maxTokens': 50}  # Keep output small
        )
        usage = resp.get('usage', {})
        return {
            'input_tokens': usage.get('inputTokens', 0),
            'output_tokens': usage.get('outputTokens', 0),
            'status': 'ok'
        }
    except Exception as e:
        return {'status': 'error', 'error': str(e)[:80]}


def run_load_test(profile, region, model_id, target_rpm, duration_seconds, prompt_tokens):
    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client('bedrock-runtime')

    interval = 60.0 / target_rpm  # seconds between requests
    total_requests = int(target_rpm * duration_seconds / 60)

    print(f"Model: {model_id}")
    print(f"Region: {region}")
    print(f"Target RPM: {target_rpm}")
    print(f"Duration: {duration_seconds}s")
    print(f"Total requests planned: {total_requests}")
    print(f"Prompt size: ~{prompt_tokens} tokens per request")
    print(f"Expected TPM: ~{target_rpm * prompt_tokens:,} tokens/min")
    print("-" * 60)

    results = {'ok': 0, 'error': 0, 'total_input_tokens': 0, 'total_output_tokens': 0}
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=min(target_rpm, 50)) as executor:
        futures = []
        for i in range(total_requests):
            # Pace requests to match target RPM
            scheduled_time = start_time + (i * interval)
            wait = scheduled_time - time.time()
            if wait > 0:
                time.sleep(wait)

            futures.append(executor.submit(invoke_model, client, model_id, prompt_tokens))

            # Print progress every 10 requests
            if (i + 1) % 10 == 0:
                elapsed = time.time() - start_time
                current_rpm = (i + 1) / elapsed * 60
                print(f"  Sent {i+1}/{total_requests} requests | {current_rpm:.0f} RPM actual | {results['ok']} ok | {results['error']} errors")

        # Collect results
        for f in as_completed(futures):
            r = f.result()
            if r['status'] == 'ok':
                results['ok'] += 1
                results['total_input_tokens'] += r.get('input_tokens', 0)
                results['total_output_tokens'] += r.get('output_tokens', 0)
            else:
                results['error'] += 1
                if results['error'] <= 3:
                    print(f"  Error: {r.get('error', 'unknown')}")

    elapsed = time.time() - start_time
    print("-" * 60)
    print(f"Done in {elapsed:.1f}s")
    print(f"Successful: {results['ok']} | Errors: {results['error']}")
    print(f"Actual RPM: {results['ok'] / elapsed * 60:.0f}")
    print(f"Total tokens: {results['total_input_tokens'] + results['total_output_tokens']:,}")
    print(f"Actual TPM: {(results['total_input_tokens'] + results['total_output_tokens']) / elapsed * 60:,.0f}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bedrock load test')
    parser.add_argument('--profile', default='default', help='AWS profile')
    parser.add_argument('--region', default='us-west-2')
    parser.add_argument('--model', default='anthropic.claude-3-haiku-20240307-v1:0')
    parser.add_argument('--rpm', type=int, default=400, help='Target requests per minute')
    parser.add_argument('--duration', type=int, default=120, help='Duration in seconds')
    parser.add_argument('--prompt-tokens', type=int, default=500, help='Approx input tokens per request')
    args = parser.parse_args()

    run_load_test(args.profile, args.region, args.model, args.rpm, args.duration, args.prompt_tokens)
