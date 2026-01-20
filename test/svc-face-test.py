#!/usr/bin/env python3
"""
Simple Creator Platform Test - NO AUTH REQUIRED
Just tests the pipeline directly without authentication complexity
"""

import requests
import time
import json

# Test the creator platform pipeline without auth
def test_creator_platform():
    print("🧪 SIMPLE CREATOR PLATFORM TEST")
    print("=" * 40)
    
    base_url = "http://localhost:8003"
    
    # Test 1: Basic health
    try:
        response = requests.get(f"{base_url}/api/health", timeout=5)
        print(f"✅ Face service health: {response.status_code}")
    except Exception as e:
        print(f"❌ Face service down: {e}")
        return
    
    # Test 2: Try creator config (this is failing per your logs)
    try:
        response = requests.get(f"{base_url}/api/face/creator/config", timeout=10)
        if response.status_code == 200:
            data = response.json()
            formats = len(data.get('image_formats', []))
            use_cases = len(data.get('use_cases', []))
            print(f"✅ Creator config: {formats} formats, {use_cases} use cases")
        else:
            print(f"❌ Creator config failed: {response.status_code}")
            print(f"   Error: {response.text[:200]}")
    except Exception as e:
        print(f"❌ Creator config error: {e}")
    
    # Test 3: Test individual endpoints that are working
    working_endpoints = [
        ("/api/face/creator/formats", "formats"),
        ("/api/face/creator/use-cases", "use_cases")
    ]
    
    for endpoint, key in working_endpoints:
        try:
            response = requests.get(f"{base_url}{endpoint}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                count = len(data.get(key, []))
                print(f"✅ {endpoint}: {count} items")
            else:
                print(f"❌ {endpoint}: {response.status_code}")
        except Exception as e:
            print(f"❌ {endpoint}: {e}")
    
    # Test 4: Try to create a job without auth (will likely fail but shows the error)
    print("\n🎨 Testing job creation (will show auth requirements):")
    
    test_request = {
        "mode": "text-to-image",
        "age_range_code": "established_professional",
        "skin_tone_code": "medium_brown",
        "region_code": "kerala",
        "gender": "female",
        "image_format_code": "instagram_portrait",
        "use_case_code": "brand_ambassador",
        "style_code": "professional",
        "num_variants": 1
    }
    
    try:
        response = requests.post(
            f"{base_url}/api/face/creator/generate",
            json=test_request,
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            job_id = data.get('job_id')
            print(f"🎉 JOB CREATED: {job_id}")
            print("   This means the field mapping is working!")
            
            # Monitor for 30 seconds
            print("   Monitoring for 30 seconds...")
            for i in range(6):
                time.sleep(5)
                try:
                    status_response = requests.get(f"{base_url}/api/face/creator/job/{job_id}/status")
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        status = status_data.get('status', 'unknown')
                        print(f"   [{i+1}/6] Status: {status}")
                        
                        if status in ['succeeded', 'failed']:
                            print(f"   🏁 Job finished: {status}")
                            break
                except:
                    print(f"   [🤷] Could not check status")
                    
        elif response.status_code == 422:
            print("❌ Validation error - field mapping issue:")
            print(f"   {response.text}")
        elif response.status_code == 401:
            print("⚠️  Authentication required (expected)")
            print("   The endpoint exists and field mapping should work")
        else:
            print(f"❌ Unexpected error: {response.status_code}")
            print(f"   {response.text[:200]}")
            
    except Exception as e:
        print(f"❌ Job creation error: {e}")
    
    print("\n📊 TEST SUMMARY:")
    print("   If you see validation errors (422), the field mapping needs fixing")
    print("   If you see auth errors (401), the endpoints work but need authentication")
    print("   If you see job creation success, the pipeline is working!")

if __name__ == "__main__":
    test_creator_platform()