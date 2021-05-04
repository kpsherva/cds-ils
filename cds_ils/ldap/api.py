# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2015-2018 CERN.
#
# cds-migrator-kit is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""CDS-ILS ldap API."""

import json
import sys
import time
import uuid
from functools import partial

import ldap
from flask import current_app
from invenio_accounts.models import User
from invenio_app_ils.errors import AnonymizationActiveLoansError
from invenio_app_ils.patrons.anonymization import anonymize_patron_data
from invenio_app_ils.patrons.indexer import PatronBaseIndexer
from invenio_app_ils.proxies import current_app_ils
from invenio_db import db
from invenio_oauthclient.models import RemoteAccount, UserIdentity
from invenio_userprofiles.models import UserProfile

from cds_ils.config import OAUTH_REMOTE_APP_NAME
from cds_ils.mail.tasks import send_warning_mail_patron_has_active_loans


def ldap_user_get(user, field_name):
    """Get first value of the given field from the LDAP user object."""
    return user[field_name][0].decode("utf8")


def ldap_user_get_email(user):
    """Get the normalized email attribute from the LDAP user object."""
    return ldap_user_get(user, "mail").lower()


class LdapClient(object):
    """Ldap client class for user importation/synchronization.

    Response example:
        [
            {'displayName': [b'Joe Foe'],
             'department': [b'IT/CDA'],
             'uidNumber': [b'100000'],
             'mail': [b'joe.foe@cern.ch'],
             'cernAccountType': [b'Primary'],
             'employeeID': [b'101010']
            },...
        ]
    """

    LDAP_BASE = "OU=Users,OU=Organic Units,DC=cern,DC=ch"

    LDAP_CERN_PRIMARY_ACCOUNTS_FILTER = "(&(cernAccountType=Primary))"

    LDAP_USER_RESP_FIELDS = [
        "mail",
        "displayName",
        "department",
        "cernAccountType",
        "employeeID",
        "uidNumber",
    ]

    def __init__(self, ldap_url=None):
        """Initialize ldap connection."""
        ldap_url = ldap_url or current_app.config["CDS_ILS_LDAP_URL"]
        self.ldap = ldap.initialize(ldap_url)

    def _search_paginated_primary_account(self, page_control):
        """Execute search to get primary accounts."""
        return self.ldap.search_ext(
            self.LDAP_BASE,
            ldap.SCOPE_ONELEVEL,
            self.LDAP_CERN_PRIMARY_ACCOUNTS_FILTER,
            self.LDAP_USER_RESP_FIELDS,
            serverctrls=[page_control],
        )

    def get_primary_accounts(self):
        """Retrieve all primary accounts from ldap."""
        page_control = ldap.controls.SimplePagedResultsControl(
            True, size=1000, cookie=""
        )

        result = []
        while True:
            response = self._search_paginated_primary_account(page_control)
            rtype, rdata, rmsgid, serverctrls = self.ldap.result3(response)
            result.extend([x[1] for x in rdata])

            ldap_page_control = ldap.controls.SimplePagedResultsControl
            ldap_page_control_type = ldap_page_control.controlType
            controls = [
                control
                for control in serverctrls
                if control.controlType == ldap_page_control_type
            ]
            if not controls:
                print("The server ignores RFC 2696 control")
                break
            if not controls[0].cookie:
                break
            page_control.cookie = controls[0].cookie

        return result

    # Kept as example if needed to fetch a specific user by a field
    # def get_user_by_person_id(self, person_id):
    #     """Query ldap to retrieve user by person id."""
    #     self.ldap.search_ext(
    #         "OU=Users,OU=Organic Units,DC=cern,DC=ch",
    #         ldap.SCOPE_ONELEVEL,
    #         "(&(cernAccountType=Primary)(employeeID={}))".format(person_id),
    #         self.LDAP_USER_RESP_FIELDS,
    #         serverctrls=[
    #             ldap.controls.SimplePagedResultsControl(
    #                 True, size=7, cookie=""
    #             )
    #         ],
    #     )
    #
    #     res = self.ldap.result()[1]
    #
    #     return [x[1] for x in res]


class LdapUserImporter:
    """Import ldap users to Invenio ILS records.

    Expected input format for ldap users:
        [
            {'displayName': [b'Joe Foe'],
             'department': [b'IT/CDA'],
             'uidNumber': [b'100000'],
             'mail': [b'joe.foe@cern.ch'],
             'cernAccountType': [b'Primary'],
             'employeeID': [b'101010']
            },...
        ]
    """

    def __init__(self):
        """Constructor."""
        self.client_id = current_app.config["CERN_APP_OPENID_CREDENTIALS"][
            "consumer_key"
        ]

    def create_invenio_user(self, ldap_user):
        """Commit new user in db."""
        email = ldap_user_get_email(ldap_user)
        user = User(email=email, active=True)
        db.session.add(user)
        db.session.commit()
        return user.id

    def create_invenio_user_identity(self, user_id, ldap_user):
        """Return new user identity entry."""
        uid_number = ldap_user_get(ldap_user, "uidNumber")
        return UserIdentity(
            id=uid_number, method=OAUTH_REMOTE_APP_NAME, id_user=user_id
        )

    def create_invenio_user_profile(self, user_id, ldap_user):
        """Return new user profile."""
        display_name = ldap_user_get(ldap_user, "displayName")
        return UserProfile(
            user_id=user_id,
            _displayname="id_{}".format(user_id),
            full_name=display_name,
        )

    def create_invenio_remote_account(self, user_id, ldap_user):
        """Return new user entry."""
        employee_id = ldap_user_get(ldap_user, "employeeID")
        department = ldap_user_get(ldap_user, "department")
        return RemoteAccount(
            client_id=self.client_id,
            user_id=user_id,
            extra_data=dict(person_id=employee_id, department=department),
        )

    def import_user(self, ldap_user):
        """Create Invenio users from LDAP export."""
        user_id = self.create_invenio_user(ldap_user)

        identity = self.create_invenio_user_identity(user_id, ldap_user)
        db.session.add(identity)

        profile = self.create_invenio_user_profile(user_id, ldap_user)
        db.session.add(profile)

        remote_account = self.create_invenio_remote_account(user_id, ldap_user)
        db.session.add(remote_account)

        return user_id


def import_users():
    """Import LDAP users in db."""
    start_time = time.time()

    ldap_client = LdapClient()
    ldap_users = ldap_client.get_primary_accounts()

    print("Users in LDAP: {}".format(len(ldap_users)))

    imported = 0
    importer = LdapUserImporter()
    for ldap_user in ldap_users:
        employee_id = ldap_user_get(ldap_user, "employeeID")
        if "mail" not in ldap_user:
            print(
                "User with employee ID {} does not have an email".format(
                    employee_id
                ),
                file=sys.stderr,
            )
            continue

        email = ldap_user_get_email(ldap_user)

        if not email:
            print("Mail field empty {}, skipping.".format(employee_id))
            continue

        if User.query.filter_by(email=email).count() > 0:
            print(
                "User with email {} already imported, skipping.".format(email)
            )
            continue

        employee_id = ldap_user_get(ldap_user, "employeeID")
        print("Importing user with person id {}".format(employee_id))
        importer.import_user(ldap_user)
        imported += 1

    db.session.commit()

    print("Users imported: {}".format(imported))
    print("Now re-indexing all patrons...")

    current_app_ils.patron_indexer.reindex_patrons()

    print("--- Finished in %s seconds ---" % (time.time() - start_time))


def _update_invenio_user(
    invenio_remote_account_id, invenio_user_profile, invenio_user, ldap_user
):
    """Check if the LDAP user has more updated info and update the Invenio."""
    invenio_user_profile.full_name = ldap_user_get(ldap_user, "displayName")
    ra = RemoteAccount.query.filter_by(id=invenio_remote_account_id).one()
    ra.extra_data["department"] = ldap_user_get(ldap_user, "department")
    invenio_user.email = ldap_user_get_email(ldap_user)


def _delete_invenio_user(user_id):
    """Delete an Invenio user."""
    try:
        anonymize_patron_data(user_id)
        return True
    except AnonymizationActiveLoansError:
        send_warning_mail_patron_has_active_loans.apply_async((user_id,))
        return False


def _log_info(log_uuid, action, extra=dict(), is_error=False):
    name = "ldap_users_synchronization"
    structured_msg = dict(name=name, uuid=log_uuid, action=action, **extra)
    structured_msg_str = json.dumps(structured_msg, sort_keys=True)
    if is_error:
        current_app.logger.error(structured_msg_str)
    else:
        current_app.logger.info(structured_msg_str)


def update_users():
    """Sync LDAP users with local users in the DB."""

    def get_ldap_users(log_func):
        """Create and return a map of all LDAP users."""
        # get all CERN users from LDAP
        ldap_client = LdapClient()
        ldap_users = ldap_client.get_primary_accounts()
        ldap_users_count = len(ldap_users)

        log_func("ldap_users_fetched", dict(users_fetched=ldap_users_count))

        ldap_users_emails = set()
        ldap_users_map = {}
        for ldap_user in ldap_users:
            # check if email exists in the user data
            if "mail" not in ldap_user:
                log_func(
                    "ldap_user_skipped_missing_email",
                    dict(employee_id=ldap_user_get(ldap_user, "employeeID")),
                )
                continue

            email = ldap_user_get_email(ldap_user)

            # check if email is empty string.
            # It happens when the account is not fully created on LDAP
            if not email:
                log_func("ldap_user_skipped_not_cern_email", dict(email=email))
                continue

            if email not in ldap_users_emails:
                ldap_person_id = ldap_user_get(ldap_user, "employeeID")
                ldap_users_map[ldap_person_id] = ldap_user
                ldap_users_emails.add(email)

        log_func("ldap_users_cached")
        return ldap_users_count, ldap_users_map, ldap_users_emails

    def remap_invenio_users(log_func):
        """Create and return a list of all Invenio users."""
        remote_accounts = RemoteAccount.query.all()
        log_func(
            "invenio_users_fetched", dict(users_fetched=len(remote_accounts))
        )

        # get all Invenio remote accounts and prepare a list with needed info
        invenio_users = []
        for remote_account in remote_accounts:
            invenio_users.append(
                dict(
                    remote_account_id=remote_account.id,
                    remote_account_person_id=remote_account.extra_data[
                        "person_id"
                    ],
                    remote_account_department=remote_account.extra_data.get(
                        "department"
                    ),
                    user_id=remote_account.user_id,
                )
            )
        log_func("invenio_users_cached")
        return invenio_users

    def update_invenio_users_from_ldap(
        invenio_users, ldap_users_map, log_func
    ):
        """Iterate on all Invenio users to update outdated info from LDAP."""
        updated_count = 0

        # Note: cannot iterate on the db query here, because when a user is
        # deleted, db session will expire, causing a DetachedInstanceError when
        # fetching the user on the next iteration
        for invenio_user in invenio_users:
            # use `dict.pop` to remove from `ldap_users_map` the users found
            # in Invenio, so the remaining will be the ones to be added
            # later on
            ldap_user = ldap_users_map.pop(
                invenio_user["remote_account_person_id"], None
            )
            if not ldap_user:
                continue

            # the imported LDAP user is already in the Invenio db
            ldap_user_display_name = ldap_user_get(ldap_user, "displayName")
            user_id = invenio_user["user_id"]
            user_profile = UserProfile.query.filter_by(user_id=user_id).one()
            invenio_full_name = user_profile.full_name

            ldap_user_department = ldap_user_get(ldap_user, "department")
            invenio_user_department = invenio_user["remote_account_department"]

            user = User.query.filter_by(id=user_id).one()
            ldap_user_email = ldap_user_get_email(ldap_user)
            invenio_user_email = user.email

            has_changed = (
                ldap_user_display_name != invenio_full_name
                or ldap_user_department != invenio_user_department
                or ldap_user_email != invenio_user_email
            )
            if has_changed:
                _update_invenio_user(
                    invenio_remote_account_id=invenio_user[
                        "remote_account_id"
                    ],
                    invenio_user_profile=user_profile,
                    invenio_user=user,
                    ldap_user=ldap_user,
                )

                log_func(
                    "department_updated",
                    dict(
                        user_id=invenio_user["user_id"],
                        previous_department=invenio_user_department,
                        new_department=ldap_user_department,
                    ),
                )

                # re-index modified patron
                patron_indexer.index(patron_cls(invenio_user["user_id"]))

                updated_count += 1

        db.session.commit()
        log_func("invenio_users_updated_from_ldap", dict(count=updated_count))

        return ldap_users_map, updated_count

    def import_new_ldap_users(new_ldap_users, log_func):
        """Import any new LDAP user not in Invenio yet."""
        importer = LdapUserImporter()
        added_count = 0
        for ldap_user in new_ldap_users:
            # Check if email already exists in Invenio.
            # Apparently, in some cases, there could be multiple LDAP users
            # with different person id but same email.
            email = ldap_user_get_email(ldap_user)
            employee_id = ldap_user_get(ldap_user, "employeeID")
            uid_number = ldap_user_get(ldap_user, "uidNumber")

            email_exists = User.query.filter_by(email=email).count() > 0

            user_identity_exists = UserIdentity.query.filter_by(
                id_user=uid_number).count() > 0

            if email_exists:
                log_func(
                    "ldap_user_skipped_email_exists_different_person_id",
                    dict(email=email, person_id=employee_id),
                )
                continue
            if user_identity_exists:
                log_func(
                    "ldap_user_skipped_identity_exists_changed_email",
                    dict(email=email, person_id=employee_id),
                )
                continue

            user_id = importer.import_user(ldap_user)
            email = ldap_user_get_email(ldap_user)
            log_func(
                "invenio_user_added",
                dict(email=email, employee_id=employee_id),
            )

            # index newly added patron
            patron_indexer.index(patron_cls(user_id))

            added_count += 1

        db.session.commit()
        log_func("import_new_users_done", dict(count=added_count))

        return added_count

    log_uuid = str(uuid.uuid4())
    log_func = partial(_log_info, log_uuid)
    start_time = time.time()

    patron_cls = current_app_ils.patron_cls
    patron_indexer = PatronBaseIndexer()

    ldap_users_count, ldap_users_map, ldap_users_emails = get_ldap_users(
        log_func
    )

    if not ldap_users_emails:
        return 0, 0, 0

    invenio_users = remap_invenio_users(log_func)

    # STEP 1 - update Invenio users with info from LDAP
    ldap_users_map, invenio_users_updated = update_invenio_users_from_ldap(
        invenio_users, ldap_users_map, log_func
    )

    # STEP 2 - import any new LDAP user not in Invenio yet
    invenio_users_added = 0
    new_ldap_users = ldap_users_map.values()
    if new_ldap_users:
        invenio_users_added = import_new_ldap_users(
            new_ldap_users, log_func
        )

    total_time = time.time() - start_time

    log_func("task_completed", dict(time=total_time))

    return (
        ldap_users_count,
        invenio_users_updated,
        invenio_users_added,
    )


def delete_users(dry_run=True):
    """Delete users that are still in the DB but not in LDAP."""
    # disabled at the moment because LDAP fetch is not reliable.
    # It happened that it returned much less users and many were automatically
    # deleted.
    raise NotImplementedError("not yet tested properly")

    invenio_users_deleted_count = 0

    # get all CERN users from LDAP
    ldap_client = LdapClient()
    ldap_users = ldap_client.get_primary_accounts()

    if not ldap_users:
        return 0, 0

    # create a map by employeeID for fast lookup
    ldap_users_map = {}
    for ldap_user in ldap_users:
        ldap_person_id = ldap_user_get(ldap_user, "employeeID")
        ldap_users_map[ldap_person_id] = ldap_user

    remote_accounts = RemoteAccount.query.all()

    # get all Invenio remote accounts and prepare a list with needed info
    invenio_users = []
    for remote_account in remote_accounts:
        invenio_users.append(
            dict(
                remote_account_id=remote_account.id,
                remote_account_person_id=remote_account.extra_data[
                    "person_id"
                ],
                remote_account_department=remote_account.extra_data.get(
                    "department"
                ),
                user_id=remote_account.user_id,
            )
        )

    for invenio_user in invenio_users:
        ldap_user = ldap_users_map.get(
            invenio_user["remote_account_person_id"]
        )
        if not ldap_user:
            # the user in Invenio does not exist in LDAP, delete it

            # fetch user and needed values before deletion
            user_id = invenio_user["user_id"]
            user = User.query.filter_by(id=user_id).one()
            email = user.email

            if not dry_run:
                success = _delete_invenio_user(user_id)
            else:
                success = True

            if success:
                invenio_users_deleted_count += 1

    if not dry_run:
        current_app_ils.patron_indexer.reindex_patrons()

    return len(ldap_users), invenio_users_deleted_count
