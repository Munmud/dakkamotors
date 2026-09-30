"""Every Cognito call the backend makes is granted by the backend's IAM policy.

Written after a missing grant reached production. `AdminInitiateAuth` was in the
policy and `AdminRespondToAuthChallenge` was not, so a customer's sign-up code
confirmed the account and then could not open the session: the endpoint answered 200
with `signed_in: false`, the app sent them to the sign-in form, and from the outside it
looked like a button that did nothing. Nothing failed loudly, which is what made it
expensive -- the tests passed, the deploy was green, and the only evidence was one
warning line in CloudWatch.

Both of those calls are gone now, with the accounts that needed them. The guard is
not: what it caught was a whole *class* of failure, where the code and the grant that
lets it run live in different files and nothing compares them. Staff administration
makes nine such calls.

Reading both files and comparing them is the cheapest guard there is: the source of
truth for what we call is `cognito.py`, and the source of truth for what we may call is
the policy in the template that grants it.
"""

import io
import pathlib
import re

from django.test import SimpleTestCase

BACKEND = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = BACKEND.parent / "infra" / "data.yaml"
COGNITO = BACKEND / "cars" / "cognito.py"

#: Calls that are not IAM actions on the pool. Empty since the public sign-up flow
#: went: every Cognito call this app makes is now an admin verb on the pool.
UNCHECKED = set()


def api_name(snake):
    """`admin_respond_to_auth_challenge` -> `AdminRespondToAuthChallenge`."""
    return "".join(part.title() for part in snake.split("_"))


def calls_in_cognito_py():
    source = io.open(COGNITO, encoding="utf-8").read()
    return {api_name(name)
            for name in re.findall(r"client\(\)\.(\w+)\(", source)} - UNCHECKED


def actions_in_policy():
    """Every `cognito-idp:Action` the backend's managed policy grants."""
    template = io.open(TEMPLATE, encoding="utf-8").read()
    start = template.index("  BackendPolicy:")
    # The policy runs to the next top-level resource. Slicing there is what keeps a
    # grant in some other role from counting -- there is no other role in this
    # template today, and the two Lambda trigger roles that used to have one are
    # exactly why this reader has never trusted that.
    end = re.compile(r"^  [A-Za-z]\w*:", re.M).search(template, start + 20).start()
    return set(re.findall(r"cognito-idp:(\w+)", template[start:end]))


class BackendPolicyTests(SimpleTestCase):
    def test_every_cognito_call_is_granted(self):
        missing = sorted(calls_in_cognito_py() - actions_in_policy())

        self.assertEqual(
            missing, [],
            f"cars/cognito.py calls {missing}, which infra/data.yaml does not grant. "
            "Add the action to BackendPolicy and deploy the data stack, or the call "
            "fails at runtime with AccessDenied and nothing else.",
        )

    def test_the_reader_finds_the_calls_and_the_grants_at_all(self):
        """Both halves are regexes over other people's files, so a rename that makes
        one of them return nothing would make the test above vacuously pass."""
        self.assertIn("AdminCreateUser", calls_in_cognito_py())
        self.assertIn("AdminAddUserToGroup", calls_in_cognito_py())
        self.assertIn("AdminCreateUser", actions_in_policy())
        self.assertGreater(len(actions_in_policy()), 10)
