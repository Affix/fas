# -*- coding: utf-8 -*-
#
# Copyright © 2008  Ricky Zhou All rights reserved.
# Copyright © 2008 Red Hat, Inc. All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program;
# if not, write to the Free Software Foundation, Inc., 51 Franklin Street,
# Fifth Floor, Boston, MA 02110-1301, USA. Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.
#
# Author(s): Ricky Zhou <ricky@fedoraproject.org>
#            Mike McGrath <mmcgrath@redhat.com>
#
import turbogears
from turbogears import controllers, expose, paginate, identity, redirect, widgets, validate, validators, error_handler, config
from turbogears.database import session
import cherrypy

import turbomail

import sqlalchemy

import os
import re
import gpgme
import StringIO
import crypt
import random
import subprocess
from OpenSSL import crypto

from sqlalchemy.sql import select, and_, not_
from fas.model import People, PeopleTable, PersonRolesTable, GroupsTable
from fas.model import Log

from fas import openssl_fas
from fas.auth import *
from fas.util import available_languages

from random import Random
import sha
from base64 import b64encode

class KnownUser(validators.FancyValidator):
    '''Make sure that a user already exists'''
    def _to_python(self, value, state):
        return value.strip()
    def validate_python(self, value, state):
        try:
            p = People.by_username(value)
        except InvalidRequestError:
            raise validators.Invalid(_("'%s' does not exist.") % value, value, state)

class UnknownUser(validators.FancyValidator):
    '''Make sure that a user doesn't already exist'''
    def _to_python(self, value, state):
        return value.strip()
    def validate_python(self, value, state):
        try:
            p = People.by_username(value)
        except InvalidRequestError:
            return
        except:
            raise validators.Invalid(_("Error: Could not create - '%s'") % value, value, state)

        raise validators.Invalid(_("'%s' already exists.") % value, value, state)

class NonFedoraEmail(validators.FancyValidator):
    '''Make sure that an email address is not @fedoraproject.org'''
    def _to_python(self, value, state):
        return value.strip()
    def validate_python(self, value, state):
        if value.endswith('@fedoraproject.org'):
            raise validators.Invalid(_("To prevent email loops, your email address cannot be @fedoraproject.org."), value, state)

class ValidSSHKey(validators.FancyValidator):
    ''' Make sure the ssh key uploaded is valid '''
    def _to_python(self, value, state):
        
        return value.file.read()
    def validate_python(self, value, state):
#        value = value.file.read()
        keylines = value.split('\n')
        for keyline in keylines:
            if not keyline:
                continue
            keyline = keyline.strip()
            m = re.match('^(rsa|ssh-rsa) [ \t]*[^ \t]+.*$', keyline)
            if not m:
                raise validators.Invalid(_('Error - Not a valid RSA SSH key: %s') % keyline, value, state)

class ValidUsername(validators.FancyValidator):
    '''Make sure that a username isn't blacklisted'''
    def _to_python(self, value, state):
        return value.strip()
    def validate_python(self, value, state):
        username_blacklist = config.get('username_blacklist')
        if re.compile(username_blacklist).match(value):
          raise validators.Invalid(_("'%s' is an illegal username.") % value, value, state)

class ValidLanguage(validators.FancyValidator):
    '''Make sure that a username isn't blacklisted'''
    def _to_python(self, value, state):
        return value.strip()
    def validate_python(self, value, state):
        if value not in available_languages():
          raise validators.Invalid(_('The language \'%s\' is not available.') % value, value, state)


class UserSave(validators.Schema):
    targetname = KnownUser
    human_name = validators.All(
        validators.String(not_empty=True, max=42),
        validators.Regex(regex='^[^\n:<>]+$'),
        )
    ssh_key = ValidSSHKey(max=5000)
    email = validators.All(
        validators.Email(not_empty=True, strip=True, max=128),
        NonFedoraEmail(not_empty=True, strip=True, max=128),
    )
    locale = ValidLanguage(not_empty=True, strip=True)
    #fedoraPersonBugzillaMail = validators.Email(strip=True, max=128)
    #fedoraPersonKeyId- Save this one for later :)
    postal_address = validators.String(max=512)
    country_code = validators.String(max=2, strip=True)

class UserCreate(validators.Schema):
    username = validators.All(
        UnknownUser,
        ValidUsername(not_empty=True),
        validators.String(max=32, min=3),
        validators.Regex(regex='^[a-z][a-z0-9]+$'),
    )
    human_name = validators.All(
        validators.String(not_empty=True, max=42),
        validators.Regex(regex='^[^\n:<>]+$'),
        )
    email = validators.All(
        validators.Email(not_empty=True, strip=True),
        NonFedoraEmail(not_empty=True, strip=True),
    )
    #fedoraPersonBugzillaMail = validators.Email(strip=True)
    postal_address = validators.String(max=512)

class UserSetPassword(validators.Schema):
    currentpassword = validators.String
    # TODO (after we're done with most testing): Add complexity requirements?
    password = validators.String(min=8)
    passwordcheck = validators.String
    chained_validators = [validators.FieldsMatch('password', 'passwordcheck')]

class UserResetPassword(validators.Schema):
    # TODO (after we're done with most testing): Add complexity requirements?
    password = validators.String(min=8)
    passwordcheck = validators.String
    chained_validators = [validators.FieldsMatch('password', 'passwordcheck')]

class UserView(validators.Schema):
    username = KnownUser

class UserEdit(validators.Schema):
    targetname = KnownUser

class SendToken(validators.Schema):
    username = KnownUser

class VerifyPass(validators.Schema):
    username = KnownUser

def generate_password(password=None, length=16):
    ''' Generate Password '''
    secret = {} # contains both hash and password

    if not password:
        # Exclude 1,l and 0,O
        chars = '23456789abcdefghijkmnopqrstuvwxyzABCDEFGHIJKLMNPQRSTUVWXYZ'
        password = ''
        for i in xrange(length):
            password += random.choice(chars)

    secret['hash'] = crypt.crypt(password, "$1$%s" % generate_salt(8))
    secret['pass'] = password

    return secret

def generate_salt(length=8):
    chars = './0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    salt = ''
    for i in xrange(length):
        salt += random.choice(chars)
    return salt

def generate_token(length=32):
    chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    token = ''
    for i in xrange(length):
        token += random.choice(chars)
    return token

class User(controllers.Controller):

    def __init__(self):
        '''Create a User Controller.
        '''

    @identity.require(turbogears.identity.not_anonymous())
    def index(self):
        '''Redirect to view
        '''
        turbogears.redirect('/user/view/%s' % turbogears.identity.current.user_name)

    def jsonRequest(self):
        return 'tg_format' in cherrypy.request.params and \
                cherrypy.request.params['tg_format'] == 'json'


    @expose(template="fas.templates.error")
    def error(self, tg_errors=None):
        '''Show a friendly error message'''
        if not tg_errors:
            turbogears.redirect('/')
        return dict(tg_errors=tg_errors)

    @identity.require(turbogears.identity.not_anonymous())
    @validate(validators=UserView())
    @error_handler(error)
    @expose(template="fas.templates.user.view", allow_json=True)
    def view(self, username=None):
        '''View a User.
        '''
        if not username:
            username = turbogears.identity.current.user_name
        person = People.by_username(username)
        if turbogears.identity.current.user_name == username:
            personal = True
        else:
            personal = False
        # TODO: We can do this without a db lookup by using something like
        # if groupname in identity.groups: pass
        # We may want to do that in auth.isAdmin() though. -Toshio
        user = People.by_username(turbogears.identity.current.user_name)
        if isAdmin(user):
            admin = True
            # TODO: Should admins be able to see personal info?  If so, enable this.  
            # Either way, let's enable this after the testing period.
            # 
            # 2008-5-14 I'd enable this in the template via
            # <py:if test='personal or admin'>click to change</py:if>
            # that way you can have different messages for an admin viewing
            # their own page via
            # <py:if test='personal'>My Account</py:if>
            # <py:if test="not personal">${user}'s Account</py:if>
            # -Toshio
            #personal = True
        else:
            admin = False
        cla = CLADone(person)
        person.jsonProps = {
                'People': ('approved_memberships', 'unapproved_memberships')
                }
        return dict(person=person, cla=cla, personal=personal, admin=admin)

    @identity.require(turbogears.identity.not_anonymous())
    @validate(validators=UserEdit())
    @error_handler(error)
    @expose(template="fas.templates.user.edit")
    def edit(self, targetname=None):
        '''Edit a user
        '''
        languages = available_languages()

        username = turbogears.identity.current.user_name
        person = People.by_username(username)

        if targetname:
            target = People.by_username(targetname)
        else:
            target = person
        if not canEditUser(person, target):
            turbogears.flash(_('You cannot edit %s') % target.username)
            turbogears.redirect('/user/view/%s' % target.username)
            return dict()
        return dict(target=target, languages=languages)

    @identity.require(turbogears.identity.not_anonymous())
    @validate(validators=UserSave())
    @error_handler(error)
    @expose(template='fas.templates.user.edit')
    def save(self, targetname, human_name, telephone, postal_address, email, ssh_key=None, ircnick=None, gpg_keyid=None, comments='', locale='en', timezone='UTC', country_code=''):
        languages = available_languages()

        username = turbogears.identity.current.user_name
        target = targetname
        person = People.by_username(username)
        target = People.by_username(target)
        emailflash = ''

        if not canEditUser(person, target):
            turbogears.flash(_("You do not have permission to edit '%s'") % target.username)
            turbogears.redirect('/user/view/%s', target.username)
            return dict()
        try:
            target.human_name = human_name
            if target.email != email:
                test = None
                try:
                    test = People.by_email_address(email)
                except:
                    pass
                if test:
                    turbogears.flash(_('Somebody is already using that email address.'))
                    return dict(target=target, languages=languages)
                else:
                    token = generate_token()
                    target.unverified_email = email
                    target.emailtoken = token
                    message = turbomail.Message(config.get('accounts_email'), email, _('Email Change Requested for %s') % person.username)
                    # TODO: Make this email friendlier. 
                    message.plain = _('''
You have recently requested to change your Fedora Account System email
to this address.  To complete the email change, you must confirm your
ownership of this email by visiting the following URL (you will need to
login with your Fedora account first):

https://admin.fedoraproject.org/accounts/user/verifyemail/%s
''') % token
                    emailflash = _('  Before your new email takes effect, you must confirm it.  You should receive an email with instructions shortly.')
                    turbomail.enqueue(message)
            target.ircnick = ircnick
            target.gpg_keyid = gpg_keyid
            target.telephone = telephone
            if ssh_key:
                target.ssh_key = ssh_key
            target.postal_address = postal_address
            target.comments = comments
            target.locale = locale
            target.timezone = timezone
            target.country_code = country_code
        except TypeError:
            turbogears.flash(_('Your account details could not be saved: %s') % e)
            return dict(target=target, languages=languages)
        else:
            turbogears.flash(_('Your account details have been saved.') + '  ' + emailflash)
            turbogears.redirect("/user/view/%s" % target.username)
            return dict()

    @identity.require(turbogears.identity.not_anonymous())
    @error_handler(error)
    @expose(template="fas.templates.user.list", allow_json=True)
    def list(self, search=u'a*'):
        '''List users

        This should be fixed up at some point.  Json data needs at least the
        following for fasClient to work::

          list of users with these attributes:
            username
            id
            ssh_key
            human_name
            password

        The template, on the other hand, needs to know about::

          list of usernames with information about whether the user is
          approved in cla_done
       
        The json information is useful so we probably want to create a new
        method for it at some point.  One which returns the list of users with
        more complete information about themselves.  Then this method can
        change to only returning username and cla status.
        '''
        ### FIXME: Should port this to a validator
        # Work around a bug in TG (1.0.4.3-2)
        # When called as /user/list/*  search is a str type.
        # When called as /user/list/?search=* search is a unicode type.
        username = turbogears.identity.current.user_name
        person = People.by_username(username)

        if not isinstance(search, unicode) and isinstance(search, basestring):
            search = unicode(search, 'utf-8', 'replace')

        re_search = search.translate({ord(u'*'): ur'%'}).lower()
        PeopleGroupsTable = PeopleTable.join(
                PersonRolesTable, PersonRoles.person_id==People.id).join(
                        GroupsTable, PersonRoles.group_id==Groups.id)

        columns = [People.username, People.id, People.human_name, People.ssh_key]
        if identity.in_group('fas-system'):
            columns.append(People.password)
        approved = select(columns, from_obj=PeopleGroupsTable
            ).where(and_(People.username.like(re_search),
                    Groups.name=='cla_done',
                    PersonRoles.role_status=='approved')
                ).distinct().order_by('username').execute()
        cla_approved = [dict(row) for row in approved]

        unapproved = select(columns).where(and_(
                People.username.like(re_search),
                not_(People.id.in_([p['id'] for p in cla_approved])))
                ).distinct().order_by('username').execute()
        cla_unapproved = [dict(row) for row in unapproved]

        if not (cla_approved or cla_unapproved):
            turbogears.flash(_("No users found matching '%s'") % search)

        return dict(people=cla_approved, unapproved_people=cla_unapproved,
                search=search)

    @identity.require(turbogears.identity.not_anonymous())
    @error_handler(error)
    @expose(format='json')
    def email_list(self, search=u'*'):
        ### FIXME: Should port this to a validator
        # Work around a bug in TG (1.0.4.3-2)
        # When called as /user/list/*  search is a str type.
        # When called as /user/list/?search=* search is a unicode type.
        if not isinstance(search, unicode) and isinstance(search, basestring):
            search = unicode(search, 'utf-8', 'replace')

        re_search = search.translate({ord(u'*'): ur'%'}).lower()
        people = People.query.filter(People.username.like(re_search)).order_by('username')
        emails = {}
        for person in people:
            emails[person.username] = person.email
        return dict(emails=emails)

    @identity.require(turbogears.identity.not_anonymous())
    @error_handler(error)
    @expose(template='fas.templates.user.verifyemail')
    def verifyemail(self, token, cancel=False):
        username = turbogears.identity.current.user_name
        person = People.by_username(username)
        if cancel:
            person.emailtoken = ''
            turbogears.flash(_('Your pending email change has been canceled.  The email change token has been invalidated.'))
            turbogears.redirect('/user/view/%s' % username)
            return dict()
        if not person.unverified_email:
            turbogears.flash(_('You do not have any pending email changes.'))
            turbogears.redirect('/user/view/%s' % username)
            return dict()
        if person.emailtoken and (person.emailtoken != token):
            turbogears.flash(_('Invalid email change token.'))
            turbogears.redirect('/user/view/%s' % username)
            return dict()
        return dict(person=person, token=token)

    @identity.require(turbogears.identity.not_anonymous())
    @error_handler(error)
    @expose()
    def setemail(self, token):
        username = turbogears.identity.current.user_name
        person = People.by_username(username)
        if not (person.unverified_email and person.emailtoken):
            turbogears.flash(_('You do not have any pending email changes.'))
            turbogears.redirect('/user/view/%s' % username)
            return dict()
        if person.emailtoken != token:
            turbogears.flash(_('Invalid email change token.'))
            turbogears.redirect('/user/view/%s' % username)
            return dict()
        ''' Log this '''
        oldEmail = person.email
        person.email = person.unverified_email
        Log(author_id=person.id, description='Email changed from %s to %s' % (oldEmail, person.email))
        person.unverified_email = ''
        session.flush()
        turbogears.flash(_('You have successfully changed your email to \'%s\'') % person.email)
        turbogears.redirect('/user/view/%s' % username)
        return dict()

    @error_handler(error)
    @expose(template='fas.templates.user.new')
    def new(self):
        if turbogears.identity.not_anonymous():
            turbogears.flash(_('No need to sign up, you have an account!'))
            turbogears.redirect('/user/view/%s' % turbogears.identity.current.user_name)
        return dict()

    @validate(validators=UserCreate())
    @error_handler(error)
    @expose(template='fas.templates.new')
    def create(self, username, human_name, email, telephone=None, postal_address=None, age_check=False):
        # TODO: Ensure that e-mails are unique?
        #       Also, perhaps implement a timeout- delete account
        #           if the e-mail is not verified (i.e. the person changes
        #           their password) withing X days.
        
        # Check that the user claims to be over 13 otherwise it puts us in a
        # legally sticky situation.
        if not age_check:
            turbogears.flash(_("We're sorry but out of special concern for children's privacy, we do not knowingly accept online personal information from children under the age of 13. We do not knowingly allow children under the age of 13 to become registered members of our sites or buy products and services on our sites. We do not knowingly collect or solicit personal information about children under 13."))
            turbogears.redirect('/')
        try:
            person = People()
            person.username = username
            person.human_name = human_name
            person.telephone = telephone
            person.email = email
            person.password = '*'
            person.status = 'active'
            session.flush()
            newpass = generate_password()
            message = turbomail.Message(config.get('accounts_email'), person.email, _('Welcome to the Fedora Project!'))
            message.plain = _('''
You have created a new Fedora account!
Your new password is: %s

Please go to https://admin.fedoraproject.org/accounts/ to change it.

Welcome to the Fedora Project. Now that you've signed up for an
account you're probably desperate to start contributing, and with that
in mind we hope this e-mail might guide you in the right direction to
make this process as easy as possible.

Fedora is an exciting project with lots going on, and you can
contribute in a huge number of ways, using all sorts of different
skill sets. To find out about the different ways you can contribute to
Fedora, you can visit our join page which provides more information
about all the different roles we have available.

http://fedoraproject.org/en/join-fedora

If you already know how you want to contribute to Fedora, and have
found the group already working in the area you're interested in, then
there are a few more steps for you to get going.

Foremost amongst these is to sign up for the team or project's mailing
list that you're interested in - and if you're interested in more than
one group's work, feel free to sign up for as many mailing lists as
you like! This is because mailing lists are where the majority of work
gets organised and tasks assigned, so to stay in the loop be sure to
keep up with the messages.

Once this is done, it's probably wise to send a short introduction to
the list letting them know what experience you have and how you'd like
to help. From here, existing members of the team will help you to find
your feet as a Fedora contributor.

And finally, from all of us here at the Fedora Project, we're looking
forward to working with you!
''') % newpass['pass']
            turbomail.enqueue(message)
            person.password = newpass['hash']
        except IntegrityError:
            turbogears.flash(_("An account has already been registered with that email address."))
            turbogears.redirect('/user/new')
            return dict()
        else:
            turbogears.flash(_('Your password has been emailed to you.  Please log in with it and change your password'))
            turbogears.redirect('/user/changepass')
            return dict()

    @identity.require(turbogears.identity.not_anonymous())
    @error_handler(error)
    @expose(template="fas.templates.user.changepass")
    def changepass(self):
        return dict()

    @identity.require(turbogears.identity.not_anonymous())
    @validate(validators=UserSetPassword())
    @error_handler(error)
    @expose(template="fas.templates.user.changepass")
    def setpass(self, currentpassword, password, passwordcheck):
        username = turbogears.identity.current.user_name
        person  = People.by_username(username)

#        current_encrypted = generate_password(currentpassword)
#        print "PASS: %s %s" % (current_encrypted, person.password)
        if not person.password == crypt.crypt(currentpassword, person.password):
            turbogears.flash('Your current password did not match')
            return dict()
        # TODO: Enable this when we need to.
        #if currentpassword == password:
        #    turbogears.flash('Your new password cannot be the same as your old one.')
        #    return dict()
        newpass = generate_password(password)
        try:
            person.password = newpass['hash']
            Log(author_id=person.id, description='Password changed')
        # TODO: Make this catch something specific.
        except:
            Log(author_id=person.id, description='Password change failed!')
            turbogears.flash(_("Your password could not be changed."))
            return dict()
        else:   
            turbogears.flash(_("Your password has been changed."))
            turbogears.redirect('/user/view/%s' % turbogears.identity.current.user_name)
            return dict()

    @error_handler(error)
    @expose(template="fas.templates.user.resetpass")
    def resetpass(self):
        if turbogears.identity.not_anonymous():
            turbogears.flash(_('You are already logged in!'))
            turbogears.redirect('/user/view/%s' % turbogears.identity.current.user_name)
        return dict()

    @error_handler(error)
    @expose(template="fas.templates.user.resetpass")
    def sendtoken(self, username, email, encrypted=False):
        import turbomail
        # Logged in
        if turbogears.identity.current.user_name:
            turbogears.flash(_("You are already logged in."))
            turbogears.redirect('/user/view/%s', turbogears.identity.current.user_name)
            return dict()
        try:
            person = People.by_username(username)
        except InvalidRequestError:
            turbogears.flash(_('Username email combo does not exist!'))
            turbogears.redirect('/user/resetpass')
        if email != person.email:
            turbogears.flash(_("username + email combo unknown."))
            return dict()
        token = generate_token()
        message = turbomail.Message(config.get('accounts_email'), email, _('Fedora Project Password Reset'))
        mail = _('''
Somebody (hopefully you) has requested a password reset for your account!
To change your password (or to cancel the request), please visit
https://admin.fedoraproject.org/accounts/user/verifypass/%(user)s/%(token)s
''') % {'user': username, 'token': token}
        if encrypted:
            # TODO: Move this out to a single function (same as
            # CLA one), think of how to make sure this doesn't get
            # full of random keys (keep a clean Fedora keyring)
            # TODO: MIME stuff?
            keyid = re.sub('\s', '', person.gpg_keyid)
            if not keyid:
                turbogears.flash(_("This user does not have a GPG Key ID set, so an encrypted email cannot be sent."))
                return dict()
            ret = subprocess.call([config.get('gpgexec'), '--keyserver', config.get('gpg_keyserver'), '--recv-keys', keyid])
            if ret != 0:
                turbogears.flash(_("Your key could not be retrieved from subkeys.pgp.net"))
                turbogears.redirect('/user/resetpass')
                return dict()
            else:
                try:
                    # This may not be the neatest fix, but gpgme gave an error when mail was unicode.
                    plaintext = StringIO.StringIO(mail.encode('utf-8'))
                    ciphertext = StringIO.StringIO()
                    ctx = gpgme.Context()
                    ctx.armor = True
                    signer = ctx.get_key(re.sub('\s', '', config.get('gpg_fingerprint')))
                    ctx.signers = [signer]
                    recipient = ctx.get_key(keyid)
                    def passphrase_cb(uid_hint, passphrase_info, prev_was_bad, fd):
                        os.write(fd, '%s\n' % config.get('gpg_passphrase'))
                    ctx.passphrase_cb = passphrase_cb
                    ctx.encrypt_sign([recipient],
                        gpgme.ENCRYPT_ALWAYS_TRUST,
                        plaintext,
                        ciphertext)
                    message.plain = ciphertext.getvalue()
                except:
                    turbogears.flash(_('Your password reset email could not be encrypted.'))
                    return dict()
        else:
            message.plain = mail;
        turbomail.enqueue(message)
        person.passwordtoken = token
        turbogears.flash(_('A password reset URL has been emailed to you.'))
        turbogears.redirect('/login')  
        return dict()

    @error_handler(error)
    @expose(template="fas.templates.user.verifypass")
    @validate(validators=VerifyPass())
    def verifypass(self, username, token, cancel=False):
        person = People.by_username(username)
        if not person.passwordtoken:
            turbogears.flash(_('You do not have any pending password changes.'))
            turbogears.redirect('/login')
            return dict()
        if person.passwordtoken != token:
            turbogears.flash(_('Invalid password change token.'))
            turbogears.redirect('/login')
            return dict()
        if cancel:
            person.passwordtoken = ''
            turbogears.flash(_('Your password reset has been canceled.  The password change token has been invalidated.'))
            turbogears.redirect('/login')
            return dict()
        return dict(person=person, token=token)

    @error_handler(error)
    @expose()
    @validate(validators=UserResetPassword())
    def setnewpass(self, username, token, password, passwordcheck):
        person = People.by_username(username)
        if not person.passwordtoken:
            turbogears.flash(_('You do not have any pending password changes.'))
            turbogears.redirect('/login')
            return dict()
        if person.passwordtoken != token:
            person.emailtoken = ''
            turbogears.flash(_('Invalid password change token.'))
            turbogears.redirect('/login')
            return dict()
        ''' Log this '''
        newpass = generate_password(password)
        person.password = newpass['hash']
        person.passwordtoken = ''
        Log(author_id=person.id, description='Password changed')
        session.flush()
        turbogears.flash(_('You have successfully reset your password.  You should now be able to login below.'))
        turbogears.redirect('/login')
        return dict()

    @identity.require(turbogears.identity.not_anonymous())
    @error_handler(error)
    @expose(template="genshi-text:fas.templates.user.cert", format="text", content_type='text/plain; charset=utf-8', allow_json=True)
    def gencert(self):
      username = turbogears.identity.current.user_name
      person = People.by_username(username) 
      if CLADone(person):
          person.certificate_serial = person.certificate_serial + 1

          pkey = openssl_fas.createKeyPair(openssl_fas.TYPE_RSA, 1024);

          digest = config.get('openssl_digest')
          expire = config.get('openssl_expire')
          cafile = config.get('openssl_ca_file')

          cakey = openssl_fas.retrieve_key_from_file(cafile)
          cacert = openssl_fas.retrieve_cert_from_file(cafile)

          req = openssl_fas.createCertRequest(pkey, digest=digest,
              C=config.get('openssl_c'),
              ST=config.get('openssl_st'),
              L=config.get('openssl_l'),
              O=config.get('openssl_o'),
              OU=config.get('openssl_ou'),
              CN=person.username,
              emailAddress=person.email,
              )

          cert = openssl_fas.createCertificate(req, (cacert, cakey), person.certificate_serial, (0, expire), digest='md5')
          certdump = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
          keydump = crypto.dump_privatekey(crypto.FILETYPE_PEM, pkey)
          return dict(cla=True, cert=certdump, key=keydump)
      else:
          if self.jsonRequest():
              return dict(cla=False)
          turbogears.flash(_('Before generating a certificate, you must first complete the CLA.'))
          turbogears.redirect('/cla/')


