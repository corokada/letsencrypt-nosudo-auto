#!/usr/bin/env python
import argparse, subprocess, json, os, urllib2, sys, base64, binascii, time, \
    hashlib, tempfile, re, copy, textwrap

def sign_csr(docroot, pubkey, csr, email=None):
    """Use the ACME protocol to get an ssl certificate signed by a
    certificate authority.

    :param string docroot: Path to the response signatures save.
    :param string pubkey: Path to the user account public key.
    :param string csr: Path to the certificate signing request.
    :param string email: An optional user account contact email
                         (defaults to webmaster@<shortest_domain>)

    :returns: Signed Certificate (PEM format)
    :rtype: string

    """
    #CA = "https://acme-staging.api.letsencrypt.org"
    CA = "https://acme-v01.api.letsencrypt.org"
    TERMS = "https://letsencrypt.org/documents/LE-SA-v1.0.1-July-27-2015.pdf"
    nonce_req = urllib2.Request("{}/directory".format(CA))
    nonce_req.get_method = lambda : 'HEAD'

    def _b64(b):
        "Shortcut function to go from bytes to jwt base64 string"
        return base64.urlsafe_b64encode(b).replace("=", "")

    # Step 1: Get account public key
    sys.stderr.write("Reading pubkey file...\n")
    proc = subprocess.Popen(["openssl", "rsa", "-pubin", "-in", pubkey, "-noout", "-text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("Error loading {}".format(pubkey))
    pub_hex, pub_exp = re.search(
        "Modulus(?: \((?:2048|4096) bit\)|)\:\s+00:([a-f0-9\:\s]+?)Exponent\: ([0-9]+)",
        out, re.MULTILINE|re.DOTALL).groups()
    pub_mod = binascii.unhexlify(re.sub("(\s|:)", "", pub_hex))
    pub_mod64 = _b64(pub_mod)
    pub_exp = int(pub_exp)
    pub_exp = "{0:x}".format(pub_exp)
    pub_exp = "0{}".format(pub_exp) if len(pub_exp) % 2 else pub_exp
    pub_exp = binascii.unhexlify(pub_exp)
    pub_exp64 = _b64(pub_exp)
    header = {
        "alg": "RS256",
        "jwk": {
            "e": pub_exp64,
            "kty": "RSA",
            "n": pub_mod64,
        },
    }
    accountkey_json = json.dumps(header['jwk'], sort_keys=True, separators=(',', ':'))
    thumbprint = _b64(hashlib.sha256(accountkey_json).digest())
    sys.stderr.write("Found public key!\n")

    # Step 2: Get the domain names to be certified
    sys.stderr.write("Reading csr file...\n")
    proc = subprocess.Popen(["openssl", "req", "-in", csr, "-noout", "-text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("Error loading {}".format(csr))
    domains = set([])
    common_name = re.search("Subject:.*? CN=([^\s,;/]+)", out)
    if common_name is not None:
        domains.add(common_name.group(1))
    subject_alt_names = re.search("X509v3 Subject Alternative Name: \n +([^\n]+)\n", out, re.MULTILINE|re.DOTALL)
    if subject_alt_names is not None:
        for san in subject_alt_names.group(1).split(", "):
            if san.startswith("DNS:"):
                domains.add(san[4:])
    sys.stderr.write("Found domains {}\n".format(", ".join(domains)))

    # Step 3: Ask user for contact email
    email = "webmaster@{}".format(min(domains, key=len))

    # Step 4: Generate the payloads that need to be signed
    # registration
    sys.stderr.write("Building request payloads...\n")
    reg_nonce = urllib2.urlopen(nonce_req).headers['Replay-Nonce']
    reg_raw = json.dumps({
        "resource": "new-reg",
        "contact": ["mailto:{}".format(email)],
        "agreement": TERMS,
    }, sort_keys=True, indent=4)
    reg_b64 = _b64(reg_raw)
    reg_protected = copy.deepcopy(header)
    reg_protected.update({"nonce": reg_nonce})
    reg_protected64 = _b64(json.dumps(reg_protected, sort_keys=True, indent=4))
    reg_file = tempfile.NamedTemporaryFile(dir=".", prefix="register_", suffix=".json")
    reg_file.write("{}.{}".format(reg_protected64, reg_b64))
    reg_file.flush()
    reg_file_name = os.path.basename(reg_file.name)
    reg_file_sig = tempfile.NamedTemporaryFile(dir=".", prefix="register_", suffix=".sig")
    reg_file_sig_name = os.path.basename(reg_file_sig.name)

    # need signature for each domain identifiers
    ids = []
    for domain in domains:
        sys.stderr.write("Building request for {}...\n".format(domain))
        id_nonce = urllib2.urlopen(nonce_req).headers['Replay-Nonce']
        id_raw = json.dumps({
            "resource": "new-authz",
            "identifier": {
                "type": "dns",
                "value": domain,
            },
        }, sort_keys=True)
        id_b64 = _b64(id_raw)
        id_protected = copy.deepcopy(header)
        id_protected.update({"nonce": id_nonce})
        id_protected64 = _b64(json.dumps(id_protected, sort_keys=True, indent=4))
        id_file = tempfile.NamedTemporaryFile(dir=".", prefix="domain_", suffix=".json")
        id_file.write("{}.{}".format(id_protected64, id_b64))
        id_file.flush()
        id_file_name = os.path.basename(id_file.name)
        id_file_sig = tempfile.NamedTemporaryFile(dir=".", prefix="domain_", suffix=".sig")
        id_file_sig_name = os.path.basename(id_file_sig.name)
        ids.append({
            "domain": domain,
            "protected64": id_protected64,
            "data64": id_b64,
            "file": id_file,
            "file_name": id_file_name,
            "sig": id_file_sig,
            "sig_name": id_file_sig_name,
        })

    # need signature for the final certificate issuance
    sys.stderr.write("Building request for CSR...\n")
    proc = subprocess.Popen(["openssl", "req", "-in", csr, "-outform", "DER"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    csr_der, err = proc.communicate()
    csr_der64 = _b64(csr_der)
    csr_nonce = urllib2.urlopen(nonce_req).headers['Replay-Nonce']
    csr_raw = json.dumps({
        "resource": "new-cert",
        "csr": csr_der64,
    }, sort_keys=True, indent=4)
    csr_b64 = _b64(csr_raw)
    csr_protected = copy.deepcopy(header)
    csr_protected.update({"nonce": csr_nonce})
    csr_protected64 = _b64(json.dumps(csr_protected, sort_keys=True, indent=4))
    csr_file = tempfile.NamedTemporaryFile(dir=".", prefix="cert_", suffix=".json")
    csr_file.write("{}.{}".format(csr_protected64, csr_b64))
    csr_file.flush()
    csr_file_name = os.path.basename(csr_file.name)
    csr_file_sig = tempfile.NamedTemporaryFile(dir=".", prefix="cert_", suffix=".sig")
    csr_file_sig_name = os.path.basename(csr_file_sig.name)

    # Step 5: Ask the user to sign the registration and requests
    cmd = "openssl dgst -sha256 -sign user.key -out %s %s" % (reg_file_sig_name, reg_file_name)
    subprocess.call(cmd.strip().split(" "))

    for i in ids:
        id_file_sig_name = i['sig_name']
        id_file_name = i['file_name']
        cmd = "openssl dgst -sha256 -sign user.key -out %s %s" % (id_file_sig_name, id_file_name)
        subprocess.call(cmd.strip().split(" "))

    cmd = "openssl dgst -sha256 -sign user.key -out %s %s" % (csr_file_sig_name, csr_file_name)
    subprocess.call(cmd.strip().split(" "))

    # Step 6: Load the signatures
    reg_file_sig.seek(0)
    reg_sig64 = _b64(reg_file_sig.read())
    for n, i in enumerate(ids):
        i['sig'].seek(0)
        i['sig64'] = _b64(i['sig'].read())

    # Step 7: Register the user
    sys.stderr.write("Registering {}...\n".format(email))
    reg_data = json.dumps({
        "header": header,
        "protected": reg_protected64,
        "payload": reg_b64,
        "signature": reg_sig64,
    }, sort_keys=True, indent=4)
    reg_url = "{}/acme/new-reg".format(CA)
    try:
        resp = urllib2.urlopen(reg_url, reg_data)
        result = json.loads(resp.read())
    except urllib2.HTTPError as e:
        err = e.read()
        # skip already registered accounts
        if "Registration key is already in use" in err:
            sys.stderr.write("Already registered. Skipping...\n")
        else:
            sys.stderr.write("Error: reg_data:\n")
            sys.stderr.write("POST {}\n".format(reg_url))
            sys.stderr.write(reg_data)
            sys.stderr.write("\n")
            sys.stderr.write(err)
            sys.stderr.write("\n")
            raise

    # Step 8: Request challenges for each domain
    responses = []
    tests = []
    for n, i in enumerate(ids):
        sys.stderr.write("Requesting challenges for {}...\n".format(i['domain']))
        id_data = json.dumps({
            "header": header,
            "protected": i['protected64'],
            "payload": i['data64'],
            "signature": i['sig64'],
        }, sort_keys=True, indent=4)
        id_url = "{}/acme/new-authz".format(CA)
        try:
            resp = urllib2.urlopen(id_url, id_data)
            result = json.loads(resp.read())
        except urllib2.HTTPError as e:
            sys.stderr.write("Error: id_data:\n")
            sys.stderr.write("POST {}\n".format(id_url))
            sys.stderr.write(id_data)
            sys.stderr.write("\n")
            sys.stderr.write(e.read())
            sys.stderr.write("\n")
            raise
        challenge = [c for c in result['challenges'] if c['type'] == "http-01"][0]
        keyauthorization = "{}.{}".format(challenge['token'], thumbprint)

        # challenge request
        sys.stderr.write("Building challenge responses for {}...\n".format(i['domain']))
        test_nonce = urllib2.urlopen(nonce_req).headers['Replay-Nonce']
        test_raw = json.dumps({
            "resource": "challenge",
            "keyAuthorization": keyauthorization,
        }, sort_keys=True, indent=4)
        test_b64 = _b64(test_raw)
        test_protected = copy.deepcopy(header)
        test_protected.update({"nonce": test_nonce})
        test_protected64 = _b64(json.dumps(test_protected, sort_keys=True, indent=4))
        test_file = tempfile.NamedTemporaryFile(dir=".", prefix="challenge_", suffix=".json")
        test_file.write("{}.{}".format(test_protected64, test_b64))
        test_file.flush()
        test_file_name = os.path.basename(test_file.name)
        test_file_sig = tempfile.NamedTemporaryFile(dir=".", prefix="challenge_", suffix=".sig")
        test_file_sig_name = os.path.basename(test_file_sig.name)
        tests.append({
            "uri": challenge['uri'],
            "protected64": test_protected64,
            "data64": test_b64,
            "file": test_file,
            "file_name": test_file_name,
            "sig": test_file_sig,
            "sig_name": test_file_sig_name,
        })

        # challenge response for server
        responses.append({
            "uri": ".well-known/acme-challenge/{}".format(challenge['token']),
            "data": keyauthorization,
        })

    # Step 9: Ask the user to sign the challenge responses
    for i in tests:
        test_file_sig_name = i['sig_name']
        test_file_name = i['file_name']
        cmd = "openssl dgst -sha256 -sign user.key -out %s %s" % (test_file_sig_name, test_file_name)
        subprocess.call(cmd.strip().split(" "))

    # Step 10: Load the response signatures
    for n, i in enumerate(ids):
        tests[n]['sig'].seek(0)
        tests[n]['sig64'] = _b64(tests[n]['sig'].read())

    # Step 11: Ask the user to host the token on their server
    for n, i in enumerate(ids):
        if not os.path.isdir("{}/.well-known/acme-challenge/".format(docroot)):
            os.makedirs("{}/.well-known/acme-challenge/".format(docroot))

        http_response_file = open("{}/{}".format(docroot, responses[n]['uri']), "w")
        http_response_file.write(responses[n]['data'])
        http_response_file.close()

        # Step 12: Let the CA know you're ready for the challenge
        sys.stderr.write("Requesting verification for {}...\n".format(i['domain']))
        test_data = json.dumps({
            "header": header,
            "protected": tests[n]['protected64'],
            "payload": tests[n]['data64'],
            "signature": tests[n]['sig64'],
        }, sort_keys=True, indent=4)
        test_url = tests[n]['uri']
        try:
            resp = urllib2.urlopen(test_url, test_data)
            test_result = json.loads(resp.read())
        except urllib2.HTTPError as e:
            sys.stderr.write("Error: test_data:\n")
            sys.stderr.write("POST {}\n".format(test_url))
            sys.stderr.write(test_data)
            sys.stderr.write("\n")
            sys.stderr.write(e.read())
            sys.stderr.write("\n")
            raise

        # Step 13: Wait for CA to mark test as valid
        sys.stderr.write("Waiting for {} challenge to pass...\n".format(i['domain']))
        while True:
            try:
                resp = urllib2.urlopen(test_url)
                challenge_status = json.loads(resp.read())
            except urllib2.HTTPError as e:
                sys.stderr.write("Error: test_data:\n")
                sys.stderr.write("GET {}\n".format(test_url))
                sys.stderr.write(test_data)
                sys.stderr.write("\n")
                sys.stderr.write(e.read())
                sys.stderr.write("\n")
                raise
            if challenge_status['status'] == "pending":
                time.sleep(2)
            elif challenge_status['status'] == "valid":
                sys.stderr.write("Passed {} challenge!\n".format(i['domain']))
                break
            else:
                raise KeyError("'{}' challenge did not pass: {}".format(i['domain'], challenge_status))

        os.remove("{}/{}".format(docroot, responses[n]['uri']))

    # Step 14: Get the certificate signed
    sys.stderr.write("Requesting signature...\n")
    csr_file_sig.seek(0)
    csr_sig64 = _b64(csr_file_sig.read())
    csr_data = json.dumps({
        "header": header,
        "protected": csr_protected64,
        "payload": csr_b64,
        "signature": csr_sig64,
    }, sort_keys=True, indent=4)
    csr_url = "{}/acme/new-cert".format(CA)
    try:
        resp = urllib2.urlopen(csr_url, csr_data)
        signed_der = resp.read()
    except urllib2.HTTPError as e:
        sys.stderr.write("Error: csr_data:\n")
        sys.stderr.write("POST {}\n".format(csr_url))
        sys.stderr.write(csr_data)
        sys.stderr.write("\n")
        sys.stderr.write(e.read())
        sys.stderr.write("\n")
        raise

    # Step 15: Convert the signed cert from DER to PEM
    signed_der64 = base64.b64encode(signed_der)
    signed_pem = """\
-----BEGIN CERTIFICATE-----
{}
-----END CERTIFICATE-----
""".format("\n".join(textwrap.wrap(signed_der64, 64)))

    return signed_pem

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""\
Get a SSL certificate signed by a Let's Encrypt (ACME) certificate authority and
output that signed certificate. You do NOT need to run this script on your
server and this script does not ask for your private keys. It will print out
commands that you need to run with your private key or on your server as root,
which gives you a chance to review the commands instead of trusting this script.

NOTE: YOUR ACCOUNT KEY NEEDS TO BE DIFFERENT FROM YOUR DOMAIN KEY.

Prerequisites:
* openssl
* python

Example: Generate an account keypair, a domain key and csr, and have the domain csr signed.
--------------
$ openssl genrsa 4096 > user.key
$ openssl rsa -in user.key -pubout > user.pub
$ openssl genrsa 4096 > domain.key
--------------single access
$ openssl req -new -sha256 -key domain.key -subj "/CN=example.com" > domain.csr
--------------dual access
$ openssl req -new -sha256 -key domain.key -subj "/" -reqexts SAN -config \
  <(cat /etc/pki/tls/openssl.cnf <(printf "[SAN]\nsubjectAltName=DNS:example.com,DNS:www.example.com")) \
  > domain.csr
$ python sign_csr.py -d /path/to/document_root -p user.pub -in domain.csr -out signed.crt
--------------

""")
    parser.add_argument("-d", "--document_root", required=True, help="path to response signatures save")
    parser.add_argument("-p", "--public-key", required=True, help="path to your account public key")
    parser.add_argument("-e", "--email", default=None, help="contact email, default is webmaster@<shortest_domain>")
    parser.add_argument("-in", "--csr_path", required=True, help="path to your certificate signing request file name")
    parser.add_argument("-out", "--crt_path", required=True, help="path to your certificate file name")

    args = parser.parse_args()
    signed_crt = sign_csr(args.document_root, args.public_key, args.csr_path, email=args.email)
    cert_file = open(args.crt_path, "w")
    cert_file.write(signed_crt)
    cert_file.flush()
    cert_file.close()
    sys.stdout.write("Create certificate file.\n")

