# remote_test
api and celery tasks for remote test execution

# tests
 - dns.check_records
   - looks for A records to match a set of states
        dns_data = {
            "akixi.altvoip.com": {
                "original": ["208.67.15.19"],
                "13056": ["208.67.15.19", "208.67.15.18"],
                "13057": ["208.67.15.18"],
            },
            "akixi.mymtm.us": {
                "original": ["208.67.14.41", "208.67.15.41", "208.67.15.42"],
                "13056": [
                    "208.67.14.41",
                    "208.67.15.41",
                    "208.67.15.42",
                    "208.67.14.42",
                    "208.67.15.39",
                    "208.67.15.38",
                ],
                "13057": ["208.67.14.42", "208.67.15.39", "208.67.15.38"],
            },
        }
        payload = {
            "task_name": "dns.check_records",
            # pass dns_data as the single positional arg; kwargs are optional
            "data": {
                "args": [dns_data],
                "kwargs": {"timeout": 3.0, "include_text": True},
            },
        }
 - apptest.run
   - runs ADP app tests, example data below in porttest.run since they use the same format
 - porttest.run
   - Checks ports for connectivity and TLS certificate
        {
            "task_name": "apptest.run", # or porttest.run
            "data": {
                "args": [
                {
                    "adp50.mymtm.us": {
                        "ports": [
                            80,
                            443,
                            2208,
                            2209,
                            8011,
                            8012
                        ],
                        "applications": [
                            "OpenClientServer",
                            "BWCallCenter",
                            "BWReceptionist",
                            "OCIOverSoap",
                            "Xsi-Actions",
                            "Xsi-Events",
                            "PublicReporting"
                        ],
                        "test_data": {
                            "user_id": "2059784479@mymtm.us",
                            "password": "******"
                        }
                    },
                    "adp52.mymtm.us": {
                        "ports": [
                            80,
                            443,
                            2208,
                            2209,
                            8011,
                            8012
                        ],
                        "applications": [
                            "OpenClientServer",
                            "BWCallCenter",
                            "BWReceptionist",
                            "OCIOverSoap",
                            "Xsi-Actions",
                            "Xsi-Events",
                            "PublicReporting"
                        ],
                        "test_data": {
                            "user_id": "2059784479@mymtm.us",
                            "password": "******"
                        }
                    },
                }
                ],
                "kwargs": {
                "timeout": 5.0,
                "verify_ssl": true
                }
            }
        }
