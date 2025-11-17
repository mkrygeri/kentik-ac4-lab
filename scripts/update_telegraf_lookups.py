#!/usr/bin/env python3
import csv, json, os, subprocess, tempfile, shutil, requests, time, pathlib, argparse, logging

CONF_PATH = "/etc/telegraf/telegraf.conf"
STORE_ID  = "mystore"
LOOKUP_DIR = pathlib.Path("/etc/telegraf/lookups")
LOOKUP_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
DEFAULT_OUTFILE = LOOKUP_DIR / "device_map.csv"
DEFAULT_CLAB_OUTFILE = LOOKUP_DIR / "clab_device_map.csv"
DEFAULT_CLAB_HOSTFILE = LOOKUP_DIR / "hosts_6030.csv"

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def get_secret(key):
    """Ask Telegraf’s CLI for a secret that sits in the same store it uses."""
    cmd = ["telegraf", "--config", CONF_PATH,
           "secrets", "get", STORE_ID, key]
    return subprocess.check_output(cmd, text=True).strip()

NETBOX_TOKEN = (get_secret("netbox_token")).split("=", 1)[1].strip()
KENTIK_TOKEN = (get_secret("kentik_token")).split("=", 1)[1].strip()
KENTIK_USER  = (get_secret("kentik_email")).split("=", 1)[1].strip()
CLAB_TOKEN   = (get_secret("containerlab_token")).split("=", 1)[1].strip()
CLAB_USER   = (get_secret("containerlab_username")).split("=", 1)[1].strip()
CLAB_PASSWORD   = (get_secret("containerlab_password")).split("=", 1)[1].strip()


NETBOX_DEV_URL  = os.getenv(
    "NETBOX_DEV_URL",
    "https://xglw4450.cloud.netboxapp.com/api/dcim/devices/?limit=0"
)
KENTIK_DEV_URL  = os.getenv(
    "KENTIK_DEV_URL",
    "https://grpc.api.kentik.com/device/v202308beta1/device?query.noCustomColumns=true"
)
CLAB_API_URL = os.getenv(
    "CLAB_API_URL",
    "http://192.168.2.203:8989/api/v1/labs?details=False"
)
CLAB_API_LOGIN_URL = os.getenv(
    "CLAB_API_URL",
    "http://192.168.2.203:8989/login"
)

def fetch_netbox():
    logging.info(f"Fetching from NetBox: {NETBOX_DEV_URL}")
    r = requests.get(NETBOX_DEV_URL,
                     headers={"Authorization": f"Token {NETBOX_TOKEN}",
                              "Accept": "application/json"},
                     timeout=30)
    r.raise_for_status()
    return r.json()["results"]

def fetch_kentik():
    logging.info(f"Fetching from Kentik: {KENTIK_DEV_URL} using user {KENTIK_USER}")
    r = requests.request("GET", KENTIK_DEV_URL,
                      data=[],
                      headers = {
                        'Accept': 'application/json',
                        'X-CH-Auth-Email': KENTIK_USER,
                        'X-CH-Auth-API-Token': KENTIK_TOKEN,
                        'Content-Type': 'application/json',
                        },
                      timeout=30)
    r.raise_for_status()
    return r.json()["devices"]
def fetch_clab_token():
    logging.info(f"Logging into containerlab: {CLAB_API_LOGIN_URL} with user {CLAB_USER} and password {CLAB_PASSWORD} ")
    payload = json.dumps({
        "password": CLAB_PASSWORD,
        "username": CLAB_USER
    })
    r = requests.post(CLAB_API_LOGIN_URL,
                      headers={"Content-Type": "application/json",
                            "Accept": "application/json"},
                            data=payload,
                            timeout=30
                            )
    r.raise_for_status()
    print(f"{r.status_code}: {r.text}")
    token   = (r.json()["token"])
    logging.info(f"Token: {token}")
    CLAB_TOKEN = token
    return token

def fetch_clab():
    token = fetch_clab_token()
    logging.info(f"Fetching from containerlab: {CLAB_API_URL} with token {token}")
    r = requests.get(CLAB_API_URL,
                     headers={
                         "Content-Type": "application/json",
                         "Authorization": f"Bearer {token}"},
                     timeout=30)
    r.raise_for_status()
    return r.json()

def write_lookup(netbox, kentik, output_path):
    tmp = tempfile.NamedTemporaryFile("w", delete=False, newline="")
    w   = csv.writer(tmp)
    w.writerow(["device","netbox_id","device_id","site","role"])
    logging.info(f"Writing to {tmp.name}")
    devices= []
    nb_index = {d["name"]: d for d in netbox}
    for k in kentik:
        if "deviceName" not in k:
            logging.warning(f"Skipping device without name: {k}")
            continue

        name = k["deviceName"]
        ip = k.get("nms",{}).get("ipAddress", "")
        if ip == "":
            logging.warning(f"Skipping device without IP: {k}")
            continue
        else:
            devices.append( ip + ":6030" )
        nb   = nb_index.get(name, {})
        #ipaddr  = nb.get("primary_ip",{}).get("address","").split("/")[0]
        w.writerow([
            name,
            nb.get("id",""),
            k.get("id",""),
            nb.get("site",{}).get("name",""),
            nb.get("device_role",{}).get("name","")
        ])
    tmp.close()
    try:
        shutil.move(tmp.name, output_path)
        logging.info(f"Wrote lookup file: {output_path}")
    except Exception as e:
        logging.error(f"Failed to move file: {e}")
        os.unlink(tmp.name)
        raise
    return devices

def write_gnmi_hosts(devices, output_path):
    print(devices)
    print(output_path)
    with open(output_path, "w") as file:
        for item in devices:
            file.write(str(item) + '\n')
    logging.info(f"Wrote gnmi hosts file: {output_path}")



def write_clab_lookup(clab_data, output_path):
    tmp = tempfile.NamedTemporaryFile("w", delete=False, newline="")
    w = csv.writer(tmp)
    w.writerow(["device","container_id","image","lab","group","owner"])
    for lab_name, nodes in clab_data.items():
        prefix = f"clab-{lab_name}-"
        for node in nodes:
            full_name = node.get("name", "")
            name = full_name.removeprefix(prefix)
            w.writerow([
                name,
                node.get("container_id",""),
                node.get("image",""),
                lab_name,
                node.get("group",""),
                node.get("owner","")
            ])
    tmp.close()
    try:
        shutil.move(tmp.name, output_path)
        logging.info(f"Wrote containerlab lookup file: {output_path}")
    except Exception as e:
        logging.error(f"Failed to write containerlab file: {e}")
        os.unlink(tmp.name)
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUTFILE, type=pathlib.Path,
                        help="Path to NetBox/Kentik output CSV lookup file")
    parser.add_argument("--clab-output", default=DEFAULT_CLAB_OUTFILE, type=pathlib.Path,
                        help="Path to containerlab output CSV lookup file")
    parser.add_argument("--gnmi-hosts", default=DEFAULT_CLAB_HOSTFILE, type=pathlib.Path,
                        help="Path to containerlab output CSV lookup file")
    args = parser.parse_args()

    try:
        netbox = fetch_netbox()
        kentik = fetch_kentik()
        clab   = fetch_clab()
        devices = write_lookup(netbox, kentik, args.output)
        write_clab_lookup(clab, args.clab_output)
        write_gnmi_hosts(devices, args.gnmi_hosts)
    except requests.RequestException as e:
        logging.error(f"API fetch failed: {e}")
        exit(1)
    except Exception as e:
        logging.error(f"Failed to write lookup files: {e}")
        exit(1)
