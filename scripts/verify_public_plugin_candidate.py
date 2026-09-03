"""Verify the exact future public-plugin candidate is free of real secrets and endpoints.

The verifier is intentionally content-free: findings contain only a relative path and
finding class, never the matched value.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

SYNTHETIC_SENTINEL_PREFIX = "SUBSTRATE_SYNTHETIC_SECRET_DO_NOT_USE_"
SYNTHETIC_SENTINEL_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_]){SYNTHETIC_SENTINEL_PREFIX}[0-9]{{24}}(?![A-Za-z0-9_])"
)
SYNTHETIC_SENTINEL_CANDIDATE_PATTERN = re.compile(rf"{SYNTHETIC_SENTINEL_PREFIX}[A-Za-z0-9_]+")
SYNTHETIC_FIXTURE_ALLOWLIST = {
    "tests/fixtures/public-plugin-secret-sentinels.json",
}
BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".webp"}
MAX_ARCHIVE_MEMBERS = 512
MAX_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100.0
URL_PATTERN = re.compile(r"https?://[^\\\s'\"<>()]+", re.IGNORECASE)
DOCUMENTATION_HOST_SUFFIXES = (
    ".invalid",
    ".test",
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
)
DOCUMENTATION_HOSTS = {
    "example.com",
    "example.net",
    "example.org",
    "github.com",
    "developercertificate.org",
    "hermes-agent.nousresearch.com",
    "json-schema.org",
    "pypi.org",
    "files.pythonhosted.org",
}
PRODUCTION_PATTERNS = (
    ("OpenAI-shaped credential", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("NVIDIA-shaped credential", re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub-shaped credential", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    (
        "Bearer credential",
        re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    ),
    (
        "Hermes API credential assignment",
        re.compile(
            r"(?<![A-Za-z0-9_])HERMES_[\"'bBrRuUfF+\s]{0,32}API_"
            r"[\"'bBrRuUfF+\s]{0,32}KEY(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)

# Exact-file exemptions are permitted only for synthetic adversarial tests. A byte
# change invalidates the exemption, so adding or replacing any credential-shaped
# value fails closed. Values are populated only after the release candidate is frozen.
SYNTHETIC_FILE_SHA256_ALLOWLIST: dict[str, frozenset[str]] = {
    "README.md": frozenset(
        {
            "0d74ffbf410fe559478ee6a67e105f275d72d0477c71e86c81531798bc07e599",
            "1cce63f45c357df4710e24a8351835c4382a47751d57edf4207e234eb350807b",
            "20ac49480369c8531e001466a6537a7dae310bbba7e6079bc217a6181e7d69d7",
            "3638d8fc00fb6328b2235dd77c00d9ba937e9b4a13cadac17f15f514f5bb9561",
            "6361beb1a5153f5dbcfca70740c1905096f3bc32ddfd4cc22157c6576c61b83a",
            "8510e5bbf41a3e79a6d5fee1f7816db734307fe7e2a7786b6a61c1ba280520a5",
            "87734abc087a4267e77aaeae7fb349d4c19f56dea2f7f3d173ab833dfb1da5e9",
            "8793ef3bbab749be6e1089034ba627d24c8b733ffd4311951b590dae01d2ca02",
            "a5285e1800831e754b648f49f1afd8b37858a2353600db497991579de7e4f323",
            "ac62cbf3799a35bbb239a0b951411491aab8ee0f2f2c33e66b8a3380a8c20e88",
            "b92ac5001e533722bd27713a39be232f25d282324179feb3e9315f1524e9c966",
            "c8eb7c5157ac7027dbf6ae86235e63dea6a2dd1faab46f0cbcbd536fbdd21ecf",
            "cdc48a8f8dbe1d68dfe329d5106c55895c5345924544c2cc4b3d7190c837705b",
        }
    ),
    "scripts/benchmark_migration.py": frozenset(
        {"ef57926174b22c2c22e73843a08a5ea9b7b45481bf51ef08dac21129ea9c929a"}
    ),
    "tests/fixtures/credential_redaction_vectors.json": frozenset(
        {"0cf55fa5cf91acdc164f2eb6936eb49af19ed9c432d060c475db4a9090cd169b"}
    ),
    "tests/test_history.py": frozenset(
        {
            "3997fc5c3b538df4ded9e994781fd8384b6b3b42ac695e1f1450ac24f6a92e7c",
            "15a6e7778a92e0d826ba1af61032f222dbf926b5ec2e189c48d7985a70824f19",
        }
    ),
    "tests/test_history_replay.py": frozenset(
        {
            "87272a47ab814803a77a39cafeb1b193b48678bc889b49712b11bace8b7d8c87",
            "226d30b1ad98ba7603bf3d8f7c86ac0857bf596bb75453d0dd57d677ec1405bd",
        }
    ),
    "tests/test_hardening.py": frozenset(
        {
            "91e53ca9b16ed43ebd49e7196bd6613f24c03cbf356f2bfc0ea03798cb065753",
            "bb9825c1889d919e29a90e43c2be48b84d846fcad813fa3896d194491cc8f386",
        }
    ),
    "tests/test_memory_provider.py": frozenset(
        {
            "c2edfbbe5a6320088f58db89b1880200b3e844ed3780885a02ef62d3218ab2b1",
            "cc967199b8e877a8088937a0c951e80f188ad4c7f29a98067bf649a2f8500b72",
        }
    ),
    "tests/test_migration_baseline.py": frozenset(
        {"0a3c7af6a761a1b22c5b94f7e1738d5d420e5f085f775d792728ed9ca684ac13"}
    ),
    "tests/test_packaging.py": frozenset(
                                   {
                                       "a3a24a5c987c939514866e72b70d01a32e34f5771089153e1fd12013829d8baf",
                                       "c4033e38f9e832779062381b0d0dcf71eecc25554d8df9af383a2d0c334dcd78",
                                       "c7a8e84d116319e62b0b7817c1a049225088c8b3baf59c42ad82cbefc3c172b5",
                                       "5793b5b96793d08060dc7faf3c39d3ee4b0717272a297f865968e84d595f4023",
                                       "e90c78ea6b182c558079c59752be07af1bc7937b69e06b66ee96a74bc8f7f0db",
                                   }
                               ),
    "plugins/substrate/README.md": frozenset({"35b846e7abdb4faad9382c015f6668d4c4bcbef1e5bb49b27b51368634cfd07c"}),
    "plugins/substrate/client.py": frozenset({"a1b0718e6e9435d0b5530bd0baa8bdb0944c47cbdd4b74c5058267ac8eed33f8"}),
    "plugins/substrate/contract.py": frozenset({"f8dabf7d1e8f0c8ce5b6f1dc3529ef5d751b6b98dec1f59705392ff099e24346"}),
    "plugins/substrate/plugin.yaml": frozenset({"86e28bce3ab3f2c1d25f09be111776c21f88c2e9f03037cfd6b3cd1f2ad9b0f4"}),
    "tests/test_publication_scanner.py": frozenset(
        {
            "c2352cc593ab08d452479f9ed59651e95acdf0cd5511a94f3830e7910bf65a21",
            "31217ebf89fbd0f13e989a611f46ce5d57a8a384f671d09bb025df2021a8d952",
            "bc7aacce24c8e18f13fa822b02e82431e8f1ae60ac00fffd4fe6bab4738197d4",
            "5b2a6acc6bf7b0ab170a58113ed62ddabb54edb8de8cdb543198346238eb42a3",
            "c68d32a864ded7a7ffc76e3037bbc3a5cf0965c980cae24094995e97e95a63d5",
            "5cb25cc2c84f8aab3de8532141aee25eb0ca5ae3ce8594fc0fb146cf46979f50",
        }
    ),
}

# Legitimate code and documentation may name the protected configuration key.
# These are exact reviewed bytes only; any modification fails closed.
EXACT_PROTECTED_KEY_REFERENCE_SHA256_ALLOWLIST: dict[str, frozenset[str]] = {
    "SECURITY.md": frozenset(
                       {
                           "34a9cd6cd747589e1ff34fe412d29a04ecc076df19871eaea8a6a078d0b17e06",
                           "48509dbbefd182381675499ff5636df18073411db580b05a1430e5eec1714df7",
                           "756264bc4dbd9f9df5c6e4bc016545fc1ac0921b384346e40d557706c2137fc5",
                           "85320ec1e6a8c655b1f4e278b95c1087a6382bf3141366f1290c0933590f4645",
                           "99aed8e5a9a712f0fd2d9caa5223f0b332220f98699351565a3bb35a23d2848c",
                           "bb4ba0542582cf3a89a37906651074c52bd1c28754b079bd67fd856c7fe24362",
                           "d07d0c65e39f0949f91f7a880a4b584b9b57d8ed3c70ca8fd95276d8d525b517",
                           "ef0cdf7a6c2fdc4ec7e781be15122f9b20331769929517a0f3c82ab15363480b",
                           "7ef28d44f7b82b765e870434542b5b67facc703e2105a40a233de2e12783852e",
                       }
                   ),
    "docs/api-ownership.json": frozenset(
        {"3112d0d252749b45c6e081c6d7987af2c7aa665936ee37eadab2ea7d01f4eb74"}
    ),
    "docs/operation.md": frozenset(
        {"e909870be827f96c4d909641ef1c834a7abad1e71f5c91b17fb8034a1900e4bd"}
    ),
    "docs/public-boundary.json": frozenset(
        {"a54d40239c5b7e3d421b3c70e6cb117dcdf67d6c018370c7321191c1391d8efe"}
    ),
    "docs/public-boundary.md": frozenset(
        {"8b62bb6463fc69c1aa8b56748df77567387c4f553ab7e7aba06617b1e43bee4f"}
    ),
    "legacy-assets/1.2.0/install_hermes_plugin.py": frozenset(
        {"bb9a8483d3d623528f573593eacebffa9483f52ecf84a452c01fa9c362b6879e"}
    ),
    "legacy-assets/1.2.0/substrate_wiki.zip": frozenset(
        {"2cbf504ec83352f23a1157777d24272b62e4b7300ad0ca991a0c4bc2e2df30b5"}
    ),
    "legacy-assets/1.3.0/install_hermes_plugin.py": frozenset(
        {"59d4d0b8557a49ec18160f4245a533465f8c4c0eb344d235af155afb8845d1b1"}
    ),
    "legacy-assets/1.3.0/substrate_wiki.zip": frozenset(
        {"6827c00444c799c085ac7a3669721d672c0d7e1e703a8a491397bb16d0655c02"}
    ),
    "legacy-assets/1.4.0/install_hermes_plugin.py": frozenset(
        {"13a05be49a83fab4c75171356d575dd85e00b27b8e09ce1602b87ae903741608"}
    ),
    "legacy-assets/1.4.0/substrate_wiki.zip": frozenset(
        {"df872d60dfc53668a0e6d30fd024e8d2f533306375980c3815ef1a483676c667"}
    ),
    "legacy-assets/1.4.1/install_hermes_plugin.py": frozenset(
        {"7600b2681c3aebcb1b1492b0a04be38bbbec637089cbbcfb1cc26e8c10865b8d"}
    ),
    "legacy-assets/1.4.1/substrate_wiki.zip": frozenset(
        {"877ccf9b0212792b699d9c98912a26980675a6050df3bd319e927639e3d901f1"}
    ),
    "scripts/install_hermes_plugin.py": frozenset(
        {
            "21ee987d4136187f020ce33ded442a9b20e97e380a2f77d4e01f6b6f1fb95617",
            "95de10e9c4e9c37c7bd3bcce1f8b507de977682e869ec4d44386f197a910554f",
            "33adb95c93f478a91991a97f0b9b6a1c9d2cee77e7894ed37fe331a4403b0bb8",
            "4b34ee40d0d08ef24d03128e1cfc5ef73c69b39ca77b3fff59f4a4133cef76f2",
            "8e37991db874b306045623c5cba39ef995b53b7b6a1a6c6f0253e2ff12c0ebb8",
        }
    ),
    "scripts/verify_public_plugin_candidate.py": frozenset(
        {
            "05e92b524fd1d08d0ff6946d9daa1fafa57c31dfe7905728d8455af88cc16759",
            "215bd6f7dab95220d03a61b998807b6ea9d18ee4f54e7b215587f40befa662ce",
            "7f650fdf6f464ba1a1698b8bb54868159823e2a17f147a1c1c43f5810464facf",
            "a421abde8bd5f3413dbd36bfc77adae6a5018ca5c62f81c94887035589e67aee",
            "eb8199baa3626770e7faf620cc73fb46022bca98f425631aff423ef8c008de27",
        }
    ),
    "src/substrate_wiki/README.md": frozenset(
        {"8612ab5974a8a5be18b2583043a7853317c9eddc7801d726d83f71220c848796"}
    ),
    "src/substrate_wiki/__init__.py": frozenset(
        {"a4143022e05a7b93d3aa5799f4319601292e67069ad85d172770c3effe0e5e9d"}
    ),
    "src/substrate_wiki/client.py": frozenset(
        {"c711b64e496214de3acf5639075bd6bada19b687cd30426058e1b0bb443dce0b",
            "f98518e2eea1d57e813130822ea95de1fcc5b550adf9b65164347468eadc6818",
            "a6e7c18e916057835e8cae9e3aa89bcc7357b397f014b8bc5bb71d995a5aa841",
            "db9eb5043a5c770f155a942459008de684378261e8fd204276d69b0bed3d788f"}
    ),
    "src/substrate_wiki/redaction.py": frozenset(
        {"e9bec198aa7ad018da359d2e9aa6df1dab717881bb41b1001348911b23e6439b"}
    ),
}


# Hosted-origin and legacy-name references are accepted only at these exact reviewed
# file digests. Any byte change re-enables all endpoint and credential-name detectors.
_HOSTED_ONBOARDING_EXACT_ALLOWLIST = {'COMPATIBILITY.md': frozenset({'1f18a49d552f2912872c10b0e41e93ff50ccd923d2fa9c108d35084dc5a5b22c',
                                '1f8fc410a88c7b410c4e72f4210226629b573a3c7f0cc410bc35939cd2317cfc',
                                '53d4873064a5d5b1748950c08c0aeaa76a6c00c435e4a8f534a844e4f90aca87',
                                '67bcb14e4988ed5b49379e93f93fa588e31a068b394a8787d1ea81ba8f6a6c70',
                                '9e2f30159b85caf7682e20d5c8abcbb1881eb2a624565ca565b93353d4bb624f',
                                'e14a7a057ee64449041a2073590641a44cfe648cff696d4d20c58eed9e4509a3'}),
 'README.md': frozenset({'0d74ffbf410fe559478ee6a67e105f275d72d0477c71e86c81531798bc07e599',
                         '20ac49480369c8531e001466a6537a7dae310bbba7e6079bc217a6181e7d69d7',
                         '6361beb1a5153f5dbcfca70740c1905096f3bc32ddfd4cc22157c6576c61b83a',
                         '87734abc087a4267e77aaeae7fb349d4c19f56dea2f7f3d173ab833dfb1da5e9',
                         'a5285e1800831e754b648f49f1afd8b37858a2353600db497991579de7e4f323',
                         'b92ac5001e533722bd27713a39be232f25d282324179feb3e9315f1524e9c966',
                         'cdc48a8f8dbe1d68dfe329d5106c55895c5345924544c2cc4b3d7190c837705b'}),
 'SECURITY.md': frozenset({'48509dbbefd182381675499ff5636df18073411db580b05a1430e5eec1714df7',
                           '756264bc4dbd9f9df5c6e4bc016545fc1ac0921b384346e40d557706c2137fc5',
                           '7ef28d44f7b82b765e870434542b5b67facc703e2105a40a233de2e12783852e',
                           '85320ec1e6a8c655b1f4e278b95c1087a6382bf3141366f1290c0933590f4645',
                           '99aed8e5a9a712f0fd2d9caa5223f0b332220f98699351565a3bb35a23d2848c',
                           '9cf9406266336996c57227413514cf97a04eb13264d80fa58c54d325fecf3d9f',
                           'd07d0c65e39f0949f91f7a880a4b584b9b57d8ed3c70ca8fd95276d8d525b517',
                           'e4457fb9df863d239120b4b4d05db31c56e5a73b4331c6bf959ed12d1f06410f',
                           'ef0cdf7a6c2fdc4ec7e781be15122f9b20331769929517a0f3c82ab15363480b'}),
 'docs/api-ownership.json': frozenset({'2fc837c1f89ef9948c9d542f4912f39414a9cd43eb650b1f73f9a2dc49b3ad2e',
                                       'f1894c4653c51c6f02d23194617a3a1e9035d2cbe2b7c282f4cd6fbb26d8b65c'}),
 'docs/operation.md': frozenset({'fadd72791e097d878bbfbf338922d568f3c3d47958f26a41f53d3597471988fa'}),
 'docs/public-boundary.json': frozenset({'648904170d6c66de6f15cfe51fb494225467730ff5167225d6fd427a6eac3571',
                                         '976403a6a802832adddd5ab1ff56fdc2e4e4847a1fe37ea5e6a2d5f106bbbbe2',
                                         'b3c24d1ccff43c274be683020f23661d45faaeb16b842af384d828c6b7968390'}),
 'docs/public-boundary.md': frozenset({'c9228e2abd22a8af558cd272988435c1caa69ad79c625cb08c8ef32e639eb0f3'}),
 'scripts/install_hermes_plugin.py': frozenset({'33adb95c93f478a91991a97f0b9b6a1c9d2cee77e7894ed37fe331a4403b0bb8',
                                                '4b34ee40d0d08ef24d03128e1cfc5ef73c69b39ca77b3fff59f4a4133cef76f2',
                                                '69af75e4240166896031f3a396fd0b2bdc4d00adbc836d1a4f22019bc6713b75',
                                                '72247d3537140098365350020cce29658c0743fee1aa738d7143db82316acce4',
                                                '8e37991db874b306045623c5cba39ef995b53b7b6a1a6c6f0253e2ff12c0ebb8',
                                                '9f157d5e7e3b8921a01d46f975f574a8955a53c5f554ed61b306a7bdfddd7309'}),
 'src/substrate_wiki/README.md': frozenset({'0c555fe0adf4f0eb7112f2897ba0ce0fb22e3d15b928608c4a67447be98c3c23',
                                            '44a11fda85d6d8170d772beadba2149bd114624479344b69a69cd49634309204',
                                            '88d492b33ee537567bf85b63dcc7a470319212ac377f4857e27e504a3a1a05a3'}),
 'src/substrate_wiki/__init__.py': frozenset({'425cd191f723805bed85d965d2d74daeac272dd0660eadf6bac92f1fa02d6f4a',
                                              '71c0c19d366759e67c7b999029777295cc8c25503d35fe9cfddc9460bcb34d5a'}),
 'src/substrate_wiki/client.py': frozenset({'082164ad24c879f6ca6434a8f28c251cd1ee7f4b413c5a788a70b351e2187f2a',
                                            '24af4976d4f4d624286261ffc7ac5901387372205ef2430999ef738938406588',
                                            '59413ab9fa2eef0d943a70a53e2b5d9d5163c0d9e154538735715e8f88585616',
                                            'a6e7c18e916057835e8cae9e3aa89bcc7357b397f014b8bc5bb71d995a5aa841',
                                            'db9eb5043a5c770f155a942459008de684378261e8fd204276d69b0bed3d788f',
                                            'dd82bf56371538a8c6fe8eb39787c5a0fb4fe1232e11fcb29b2d4ac11f0bd9ed',
                                            'f98518e2eea1d57e813130822ea95de1fcc5b550adf9b65164347468eadc6818'}),
 'src/substrate_wiki/onboarding.py': frozenset({'2f1d35c0c568a6d817a3cfcb36b99a4447636ed8ab512d656a4c4411f4a48967',
                                                '4ae1938346a5af64b3936913c4b8438891d8170c6caa617b2eb3b5c816079ec0',
                                                '52e11806e20017c920d4c0caef394570a8fdfb2cabff8a87cdbde3dc7e5a64fc',
                                                '73c8c6669b3c052578ce4818cd336b2f871794bcdb28a42131a856174de660b7',
                                                '9b8b300f6ea4130442ae1838e841c63019877943d1bc8a84378c7008389add17',
                                                'a835092092bca7266978609763d36f5767ac363fe7180bda482d713df2ecc439',
                                                'ac9934df2970093eb94d1fe5fb820c025d5f3fec2a32fdc5346e77ad9df66626',
                                                'd58fc9693f78cb4d5f8d9738c9895f009e098d73400d4e9530e7454662fabff1',
                                                'da01090b5f007d9741a06d7ff4a9c8140036e4ef26512665b4802800e0b543a3'}),
 'tests/test_hardening.py': frozenset({'bb9825c1889d919e29a90e43c2be48b84d846fcad813fa3896d194491cc8f386',
                                       'f5f87125f1edd37bff1d44301d6bb0f44cc7faf3ba6122bdbe7569f349fea7a3'}),
 'tests/test_memory_provider.py': frozenset({'2c4517847dfad341063a69afcc737316a74574471fc98a45eaff21cfe4e271fd',
                                             'cc967199b8e877a8088937a0c951e80f188ad4c7f29a98067bf649a2f8500b72'}),
 'plugins/substrate/onboarding.py': frozenset({'19708eb1ce0cb4ddc5a64523cad351d0794ce4c8397079add2098c3905a66629'}),
 'tests/test_retrieval_onboarding.py': frozenset({'866b7637fcdb8120b22fe2c822b18e7b211d4dc447e702af47cb2bd89b343127'}),
 'plugins/substrate/plugin.py': frozenset({'b9004cde31173ad2037c7b21b5975b05b7360efed5ae4f07a78594ed037ff3c7'}),
 'plugins/substrate/README.md': frozenset({'dff165ab8024d2788d52e4653e57c425cf98a8f24d1c13a5b3b0bead38287c40'}),
 'plugins/substrate/client.py': frozenset({'61ece2e3c21517d34341a1df13fb6782e844100c25c1020c679b62e28c22885b'}),
 'plugins/substrate/setup.py': frozenset({'caec7593ba38e6849ec65d4ba1e1951a35e07fdcc68dd5f32df335b75554f202'}),
 'tests/test_retrieval_plugin.py': frozenset({'768cbaefad2efcf49eaf24772410ddbf03373b54714113bc392e375626ea5b49'}),
 'tests/test_retrieval_setup.py': frozenset({'109b1721f12223493aec3528703c7c65db1b58355efa6c06f04821ddb9c0fe33'}),
}
for _path, _digests in _HOSTED_ONBOARDING_EXACT_ALLOWLIST.items():
    SYNTHETIC_FILE_SHA256_ALLOWLIST[_path] = (
        SYNTHETIC_FILE_SHA256_ALLOWLIST.get(_path, frozenset()) | _digests
    )

# This digest freezes the independently reviewed source/destination/class boundary
# separately from per-file hashes in the mutable extraction manifest. It covers
# only that sorted policy projection, so ordinary byte changes do not change
# policy. Adding, moving, or reclassifying a file requires explicit review.
TRUSTED_INVENTORY_POLICY_SHA256 = (
    "a36a7a604d07d2478936c8f614d13adf41a34b41a8873e792338a03579a12884"
)
TRUSTED_HISTORICAL_BLOB_POLICY_SHA256 = (
    "a32d829aa4f52f2cb18148c45dabfbad75dee74242bfef177c09e8c936dbf73b"
)
SCANNER_PATH = "scripts/verify_public_plugin_candidate.py"
DESTINATION_MANIFEST_PATH = "docs/extraction-manifest.json"


def _normalize_historical_policy_payload(relative_path: str, payload: bytes) -> bytes:
    """Remove the history policy's intentional self-reference before hashing."""

    if relative_path == SCANNER_PATH:
        text = payload.decode("utf-8")
        text, count = re.subn(
            r'(?s)(TRUSTED_HISTORICAL_BLOB_POLICY_SHA256\s*=\s*\(\s*")[0-9a-f]{64}("\s*\))',
            lambda match: match.group(1) + "0" * 64 + match.group(2),
            text,
            count=1,
        )
        return text.encode("utf-8") if count else payload
    if relative_path == DESTINATION_MANIFEST_PATH:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return payload
        normalized = 0
        for item in value.get("entries", []):
            if isinstance(item, dict) and item.get("destination") == SCANNER_PATH:
                item["destination_sha256"] = "0" * 64
                normalized += 1
        for item in value.get("destination_only", []):
            if isinstance(item, dict) and item.get("path") == SCANNER_PATH:
                item["sha256"] = "0" * 64
                normalized += 1
        if normalized == 1:
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload


def _publication_tree_roots(root: Path) -> list[tuple[str, str]]:
    """Return every commit tree and every ref recursively peeled to a tree."""

    roots = {
        (f"commit:{commit}", commit)
        for commit in _git(root, "rev-list", "--all").decode("ascii").splitlines()
    }
    refs = _git(root, "for-each-ref", "--format=%(refname)").decode("utf-8").splitlines()
    for ref in refs:
        object_id = _git(root, "rev-parse", f"{ref}^{{}}").decode("ascii").strip()
        object_type = _git(root, "cat-file", "-t", object_id).decode("ascii").strip()
        if object_type == "tree":
            roots.add((f"tree:{object_id}", object_id))
    return sorted(roots)


def _historical_blob_policy_sha256(root: Path, manifest: dict[str, Any]) -> str:
    """Hash every distinct path/class/mode/content tuple across published refs."""

    path_classes = {
        item["destination"]: item["class"] for item in manifest["entries"]
    }
    path_classes.update(
        {item["path"]: item["class"] for item in manifest["destination_only"]}
    )
    path_classes[manifest["self_excluded_path"]] = "closed-inventory-manifest"
    projection: set[tuple[str, str, str, str]] = set()
    for _, treeish in _publication_tree_roots(root):
        for raw_entry in (
            entry for entry in _git(root, "ls-tree", "-r", "-z", treeish).split(b"\0") if entry
        ):
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, raw_object_id = metadata.split(b" ", 2)
            relative_path = raw_path.decode("utf-8")
            object_id = raw_object_id.decode("ascii")
            if object_type == b"blob":
                payload = _git(root, "cat-file", "blob", object_id)
                normalized_digest = hashlib.sha256(
                    _normalize_historical_policy_payload(relative_path, payload)
                ).hexdigest()
            else:
                normalized_digest = object_id
            projection.add(
                (
                    relative_path,
                    path_classes.get(relative_path, "outside-closed-inventory"),
                    mode.decode("ascii"),
                    f"{object_type.decode('ascii')}:{normalized_digest}",
                )
            )
    payload = json.dumps(sorted(projection), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolved_python_strings(text: str) -> set[str]:
    """Resolve bounded constant string assignments without executing candidate code."""

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
        key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
    )
    values: dict[str, str] = {}

    def resolve(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = resolve(node.left)
            right = resolve(node.right)
            if left is not None and right is not None and len(left) + len(right) <= 256:
                return left + right
        return None

    for _ in range(min(len(assignments) + 1, 64)):
        changed = False
        for assignment in assignments:
            value = resolve(assignment.value)
            if value is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if isinstance(target, ast.Name) and values.get(target.id) != value:
                    values[target.id] = value
                    changed = True
        if not changed:
            break
    return set(values.values())


def scan_text(relative_path: str, text: str) -> list[str]:
    """Return content-free findings for one candidate text file."""

    findings: list[str] = []
    sentinels = SYNTHETIC_SENTINEL_PATTERN.findall(text)
    sentinel_candidates = SYNTHETIC_SENTINEL_CANDIDATE_PATTERN.findall(text)
    if len(sentinel_candidates) != len(sentinels):
        findings.append(f"{relative_path}: malformed synthetic sentinel")
    if sentinels and relative_path not in SYNTHETIC_FIXTURE_ALLOWLIST:
        findings.append(f"{relative_path}: synthetic sentinel outside exact fixture allowlist")
    production_text = SYNTHETIC_SENTINEL_PATTERN.sub("[SYNTHETIC-SENTINEL]", text)
    production_text = re.sub(
        r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))",
        lambda match: chr(int(match.group(1) or match.group(2), 16))
        if int(match.group(1) or match.group(2), 16) < 128
        else match.group(0),
        production_text,
    )
    for label, pattern in PRODUCTION_PATTERNS:
        if relative_path == SCANNER_PATH and label == "Hermes API credential assignment":
            continue
        if pattern.search(production_text):
            findings.append(f"{relative_path}: {label}")
    # The scanner necessarily contains protected-name detector fixtures. Its exact bytes
    # are bound separately by the manifest and normalized all-public-root policy.
    if relative_path != SCANNER_PATH and any(
        value.casefold() == "hermes_api_key" for value in _resolved_python_strings(text)
    ):
        findings.append(f"{relative_path}: Hermes API credential assignment")
    for value in URL_PATTERN.findall(production_text):
        try:
            hostname = (urlsplit(value).hostname or "").casefold().rstrip(".")
        except ValueError:
            findings.append(f"{relative_path}: production-shaped HTTP(S) endpoint")
            continue
        allowed = (
            hostname in {"localhost", "127.0.0.1", "::1"}
            or hostname in DOCUMENTATION_HOSTS
            or hostname.endswith(DOCUMENTATION_HOST_SUFFIXES)
        )
        if not allowed:
            findings.append(f"{relative_path}: production-shaped HTTP(S) endpoint")
    return findings


def scan_archive(relative_path: str, archive: Path) -> list[str]:
    """Inspect every bounded UTF-8 member of a ZIP candidate."""

    findings: list[str] = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return [f"{relative_path}: archive member count exceeds bound"]
            total_bytes = 0
            for member in members:
                member_path = PurePosixPath(member.filename)
                target = f"{relative_path}!/{member.filename}"
                if member.flag_bits & 0x1:
                    findings.append(f"{target}: encrypted archive member")
                    continue
                if member_path.is_absolute() or ".." in member_path.parts:
                    findings.append(f"{target}: unsafe archive path")
                    continue
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    findings.append(f"{target}: archive symlink is forbidden")
                    continue
                if member.is_dir():
                    continue
                total_bytes += member.file_size
                if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                    return sorted(findings + [f"{relative_path}: archive size exceeds bound"])
                if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    findings.append(f"{target}: archive member size exceeds bound")
                    continue
                ratio = member.file_size / max(member.compress_size, 1)
                if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                    findings.append(f"{target}: archive compression ratio exceeds bound")
                    continue
                try:
                    payload = bundle.read(member)
                except RuntimeError:
                    findings.append(f"{target}: unreadable archive member")
                    continue
                if member_path.suffix.casefold() in BINARY_SUFFIXES:
                    text = payload.decode("latin-1")
                else:
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        findings.append(f"{target}: unexpected non-UTF-8 archive member")
                        text = payload.decode("latin-1")
                findings.extend(scan_text(target, text))
    except (OSError, zipfile.BadZipFile):
        return [f"{relative_path}: invalid ZIP archive"]
    return sorted(findings)


def _scan_bytes(relative_path: str, payload: bytes) -> list[str]:
    """Scan one ordinary file or bounded ZIP payload."""

    digest = hashlib.sha256(payload).hexdigest()
    allowed_digests = SYNTHETIC_FILE_SHA256_ALLOWLIST.get(
        relative_path, frozenset()
    ) | EXACT_PROTECTED_KEY_REFERENCE_SHA256_ALLOWLIST.get(relative_path, frozenset())
    if digest in allowed_digests:
        return []
    if Path(relative_path).suffix.casefold() == ".zip":
        with tempfile.NamedTemporaryFile(suffix=".zip") as stream:
            stream.write(payload)
            stream.flush()
            return scan_archive(relative_path, Path(stream.name))
    if Path(relative_path).suffix.casefold() in BINARY_SUFFIXES:
        text = payload.decode("latin-1")
    else:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return [f"{relative_path}: unexpected non-UTF-8 candidate file"]
    return scan_text(relative_path, text)


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ("git", "-C", str(root), *args),
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("candidate Git inventory is unavailable") from exc


def _nul_paths(payload: bytes) -> list[str]:
    return [value.decode("utf-8") for value in payload.split(b"\0") if value]


def _all_candidate_paths(root: Path) -> set[str]:
    """Enumerate every file/symlink outside Git metadata, including ignored files."""

    paths: set[str] = set()
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        if directory_path == root and ".git" in names:
            names.remove(".git")
        for name in (*names, *files):
            path = directory_path / name
            if path.is_file() or path.is_symlink():
                paths.add(path.relative_to(root).as_posix())
    return paths


def _scan_destination_repository(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Scan the closed working tree plus every blob reachable from a Git ref."""

    findings: list[str] = []
    try:
        inventory_policy = {
            "entries": sorted(
                (
                    {
                        "source": item["source"],
                        "destination": item["destination"],
                        "class": item["class"],
                    }
                    for item in manifest["entries"]
                ),
                key=lambda item: item["destination"],
            ),
            "destination_only": sorted(
                (
                    {"path": item["path"], "class": item["class"]}
                    for item in manifest["destination_only"]
                ),
                key=lambda item: item["path"],
            ),
        }
        policy_payload = json.dumps(
            inventory_policy, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (KeyError, TypeError):
        findings.append("candidate: invalid closed inventory path/class policy")
    else:
        if hashlib.sha256(policy_payload).hexdigest() != TRUSTED_INVENTORY_POLICY_SHA256:
            findings.append("candidate: closed inventory path/class policy mismatch")
    tracked = set(_nul_paths(_git(root, "ls-files", "-z")))
    unexpected = _all_candidate_paths(root) - tracked
    for relative_path in sorted(unexpected):
        findings.append(f"{relative_path}: unexpected untracked candidate file")
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            findings.append(f"{relative_path}: unsafe or missing untracked file")
            continue
        findings.extend(_scan_bytes(relative_path, path.read_bytes()))

    sources: set[str] = set()
    destinations: set[str] = set()
    for item in manifest["entries"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source"), str)
            or not isinstance(item.get("destination"), str)
        ):
            raise ValueError("invalid destination inventory entry")
        source_path = item["source"]
        relative_path = item["destination"]
        if source_path in sources:
            findings.append(f"{source_path}: duplicate source inventory entry")
        if relative_path in destinations:
            findings.append(f"{relative_path}: duplicate destination inventory entry")
        sources.add(source_path)
        destinations.add(relative_path)
        expected_digest = item.get("destination_sha256")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            findings.append(f"{relative_path}: missing destination SHA-256")
        elif (root / relative_path).is_file() and hashlib.sha256(
            (root / relative_path).read_bytes()
        ).hexdigest() != expected_digest:
            findings.append(f"{relative_path}: destination SHA-256 mismatch")

    destination_only = manifest.get("destination_only")
    if not isinstance(destination_only, list):
        raise ValueError("destination-only inventory is missing")
    destination_only_paths: set[str] = set()
    for item in destination_only:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("invalid destination-only inventory entry")
        relative_path = item["path"]
        if relative_path in destination_only_paths:
            findings.append(f"{relative_path}: duplicate destination-only inventory entry")
        if relative_path in destinations:
            findings.append(f"{relative_path}: path appears in both destination inventories")
        destination_only_paths.add(relative_path)
        expected_digest = item.get("sha256")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            findings.append(f"{relative_path}: missing destination-only SHA-256")
        elif (root / relative_path).is_file() and hashlib.sha256(
            (root / relative_path).read_bytes()
        ).hexdigest() != expected_digest:
            findings.append(f"{relative_path}: destination-only SHA-256 mismatch")

    self_path = manifest.get("self_excluded_path")
    if not isinstance(self_path, str):
        raise ValueError("manifest self-exclusion is missing")
    expected_tracked = destinations | destination_only_paths | {self_path}
    findings.extend(
        f"{path}: tracked candidate path is absent from closed inventory"
        for path in sorted(tracked - expected_tracked)
    )
    findings.extend(
        f"{path}: closed inventory path is not tracked"
        for path in sorted(expected_tracked - tracked)
    )

    for relative_path in sorted(tracked):
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            findings.append(f"{relative_path}: unsafe or missing tracked file")
            continue
        findings.extend(_scan_bytes(relative_path, path.read_bytes()))

    tree_blob_ids: set[str] = set()
    for root_label, treeish in _publication_tree_roots(root):
        tree_entries = _git(root, "ls-tree", "-r", "-z", treeish).split(b"\0")
        for raw_entry in (entry for entry in tree_entries if entry):
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, raw_object_id = metadata.split(b" ", 2)
            relative_path = raw_path.decode("utf-8")
            object_id = raw_object_id.decode("ascii")
            tree_blob_ids.add(object_id)
            if relative_path not in expected_tracked:
                findings.append(
                    f"git:{root_label}:{relative_path}: historical path outside closed inventory"
                )
            if object_type != b"blob" or mode not in {b"100644", b"100755"}:
                findings.append(
                    f"git:{root_label}:{relative_path}: historical entry is not a regular file"
                )
            for finding in scan_text(f"git-path:{root_label}", relative_path):
                _, label = finding.rsplit(": ", 1)
                findings.append(f"git:{root_label}:{relative_path}: {label}")
            if object_type != b"blob":
                continue
            payload = _git(root, "cat-file", "blob", object_id)
            for finding in _scan_bytes(relative_path, payload):
                _, label = finding.split(": ", 1)
                findings.append(f"git:{root_label}:{relative_path}: {label}")

    try:
        historical_policy_digest = _historical_blob_policy_sha256(root, manifest)
    except (KeyError, TypeError, ValueError):
        findings.append("candidate: invalid historical blob policy")
    else:
        if historical_policy_digest != TRUSTED_HISTORICAL_BLOB_POLICY_SHA256:
            findings.append("candidate: historical blob policy mismatch")

    reachable_objects = {
        line.split(b" ", 1)[0].decode("ascii")
        for line in _git(root, "rev-list", "--objects", "--all").splitlines()
        if line
    }
    for object_id in sorted(reachable_objects):
        object_type = _git(root, "cat-file", "-t", object_id).decode("ascii").strip()
        if object_type not in {"blob", "commit", "tag"}:
            continue
        if object_type == "blob" and object_id in tree_blob_ids:
            continue
        payload = _git(root, "cat-file", object_type, object_id)
        for finding in _scan_bytes(f"git-object:{object_id}:{object_type}.txt", payload):
            _, label = finding.rsplit(": ", 1)
            findings.append(f"git-object:{object_id}:{object_type}: {label}")
    return sorted(set(findings))


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError("invalid public plugin move manifest")
    return value


def scan_candidate(
    root: Path,
    manifest_path: Path,
    *,
    layout: Literal["auto", "source", "destination"] = "auto",
) -> list[str]:
    """Scan the closed candidate repository or the exact embedded source manifest."""

    manifest = _load_manifest(manifest_path)
    findings: list[str] = []
    seen: set[str] = set()
    entries = manifest["entries"]
    if layout == "auto":
        if all(
            isinstance(item, dict) and (root / str(item.get("source"))).is_file()
            for item in entries
        ):
            selected_layout = "source"
        elif all(
            isinstance(item, dict) and (root / str(item.get("destination"))).is_file()
            for item in entries
        ):
            selected_layout = "destination"
        else:
            return ["candidate: neither source nor destination layout is complete"]
    else:
        selected_layout = layout
    if selected_layout == "destination":
        return _scan_destination_repository(root, manifest)
    for item in entries:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source"), str)
            or not isinstance(item.get("destination"), str)
        ):
            raise ValueError("invalid public plugin move entry")
        relative_path = item[selected_layout]
        if relative_path in seen:
            raise ValueError("duplicate public plugin move source")
        seen.add(relative_path)
        source = root / relative_path
        if not source.is_file():
            findings.append(f"{relative_path}: missing manifest source")
            continue
        expected_digest = item.get("source_sha256")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            findings.append(f"{relative_path}: missing source SHA-256")
        elif hashlib.sha256(source.read_bytes()).hexdigest() != expected_digest:
            findings.append(f"{relative_path}: source SHA-256 mismatch")
        findings.extend(_scan_bytes(relative_path, source.read_bytes()))
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--layout", choices=("auto", "source", "destination"), default="auto")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.manifest is None:
        candidates = (
            root / "docs" / "extraction-manifest.json",
            root / "docs" / "file-move-manifest.json",
        )
        manifest = next(
            (candidate for candidate in candidates if candidate.is_file()), candidates[0]
        )
    else:
        manifest = args.manifest if args.manifest.is_absolute() else root / args.manifest
    findings = scan_candidate(root, manifest, layout=args.layout)
    print(json.dumps({"findings": findings, "status": "pass" if not findings else "fail"}))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
