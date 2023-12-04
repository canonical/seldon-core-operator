import os
import json 

def get_integration_environments() -> None:

    tox_environments=[]

    seldon_servers = os.listdir("./tests/assets/crs/test-seldon-servers/")
    seldon_servers = [s.replace(".yaml","") for s in seldon_servers]
    # Add "seldon-servers-integration --  -k"
    tox_environments = [ "seldon-servers-integration --  -k " + s for s in seldon_servers]
    tox_environments.append("charm-integration --")

    environments=json.dumps(tox_environments)
    print(
        f"The following environments have been created: {environments}")
    with open(os.environ['GITHUB_OUTPUT'], 'a') as fh:
        print(f'environments={environments}', file=fh)

get_integration_environments()
