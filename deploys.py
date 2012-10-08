#! /usr/bin/python
# coding=utf-8

import sys, os, traceback
import ConfigParser

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import urllib2, cookielib
import base64, json, re

import shelve

class service:
    def __init__(self, config):
        self._config = config

    def _request(self, *args):
        if type(args[0]) == type(''):
            args = list(args)
            args[0] = self._config['base_url'] + args[0]
            args = tuple(args)

        request = urllib2.Request(*args)

        loginBase64 = base64.b64encode('%(username)s:%(password)s' % self._config)
        request.add_header('Authorization', 'Basic %s' % loginBase64)

        response = urllib2.urlopen(request)
        response._headers = response.info().dict

        if response._headers.has_key('content-type') and response._headers['content-type'].find('application/json') != -1:
            response._decode = json.load(response)

        return response

class jira(service):
    def get_projects_code(self):
        response = self._request('project')
        return [project['key'] for project in response._decode]

    def get_task_title(self, task_id):
        try:
            response = self._request('issue/%s?fields=summary' % (task_id))

            if not response._decode.has_key('fields') or not response._decode['fields'].has_key('summary'):
                return None
        except:
            return None

        return response._decode['fields']['summary']

class github(service):
    def get_compare_commits(self, project, base, head):
        response = self._request('repos/%s/%s/compare/%s...%s' % (project['owner'], project['repo'], base, head))

        commits = []
        for commit in response._decode['commits']:
            if len(commit['parents']) == 1:
                commits.append({
                    'sha': commit['sha'],
                    'message': commit['commit']['message'],
                    'url': 'https://github.com/%s/%s/commit/%s' % (project['owner'], project['repo'], commit['sha']),
                    'date': commit['commit']['committer']['date']
                })
        commits.sort(key=lambda i:i['date'], reverse=True)

        return commits

class report():
    def __init__(self, config):
        self._mail = smtplib.SMTP('localhost')
        self._config = config

        debug = int(config['debug'])
        if debug > 0:
            self._mail.set_debuglevel(debug)

    def commits(self, project, commits):
        show = []
        body = '''<table><thead><tr><th>Github commit</th><th>Jira task</th><th>Description</th></tr></thead><tbody>
              <tfoot><tr><td colspan="3" align="right"><strong>Total commits:</strong> %s</td><tr></tfoot>'''\
               % (len(commits))

        for commit in commits:
            if commit.has_key('task') and commit.has_key('title'):
                description = commit['title']
                tack = '<a href="http://jira.local/browse/%(task)s">%(task)s</a>' % commit
            else:
                description = commit['message']
                tack = ''

            if description in show:
                continue

            show.append(description)
            body += '<tr><td><a href="%s">Show commit</a></td><td>%s</td><td>%s</td></tr>'\
                    % (commit['url'], tack, description)

        body += '</tbody></table>'
        self._send(self._config['mail_report'], 'Deploy ' + project['title'], body, project)

    def rollback(self, project):
        self._send(self._config['mail_report_error'], 'Rollback ' + project['title'], 'Rollback', project)

    def error(self, project, message):
        if project.has_key('base') and project.has_key:
            message = '<strong>Base commit:</strong> %s<br /><strong>Head commit:</strong> %s<br /><pre>%s</pre>'\
                      % (project['base'], project['head'], message)
        else:
            message = '<pre>%s<pre>' % message

        self._send(self._config['mail_report_error'], 'Error ' + project['title'], message, project)

    def _send(self, to, subject, body, project):
        body = '<h3>Project %s</h3>' % project['title'] + body

        msg = MIMEMultipart('alternative')
        msg['From'] = self._config['mail_report_from']
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body.encode('utf-8'), 'html', 'utf-8'))

        self._mail.sendmail(msg['From'], msg['To'], msg.as_string())
        self._mail.quit()

def main():
    path = os.path.dirname(os.path.abspath(__file__)) + '/'

    config = ConfigParser.ConfigParser()
    config.read(path + 'config.ini')

    general_cfg = dict(config.items('general'))
    debug = int(general_cfg['debug'])

    projects = [];
    for section in config.sections():
        if section[0:8] != 'project-':
            continue

        projects.append(dict(config.items(section)))

    cache = shelve.open(path + general_cfg['cache_file'])

    cookie_handler = urllib2.HTTPCookieProcessor(cookielib.CookieJar())
    opener = urllib2.build_opener(cookie_handler)

    if debug > 0:
        debug_https_handler = urllib2.HTTPSHandler(debuglevel=debug)
        debug_http_handler = urllib2.HTTPHandler(debuglevel=debug)
        opener = urllib2.build_opener(cookie_handler, debug_http_handler, debug_https_handler)

    urllib2.install_opener(opener)

    jira_client = jira(dict(config.items('jira')))
    github_client = github(dict(config.items('github')))

    for project in projects:
        try:
            version_encode = urllib2.urlopen(project['url']).readline()
            match = re.compile(r'Version:.*?-([a-z0-9]{10})').match(version_encode)

            if not match:
                raise Exception('Error parse file ' + project['url'])

            head = match.group(1)
            base = project['last_base']

            changed = (not cache.has_key(project['url']) or cache[project['url']] != head) and head != base

            if not changed:
                continue

            if cache.has_key(project['url']):
                base = cache[project['url']]

            base = base[0:len(head)]
            cache[project['url']] = head

            project['base'] = base
            project['head'] = head

            commits = github_client.get_compare_commits(project, base, head)

            if not commits:
                report(general_cfg).rollback(project)
            else:
                if 'pattern_task' not in locals():
                    codes = '|'.join(map(str, jira_client.get_projects_code()))
                    pattern_task = re.compile(r'.*?(' + codes + ')[\s_-]+([0-9]+).*', re.I)

                show_tasks = []
                for commit in commits:
                    commit['show'] = True

                    match = pattern_task.match(commit['message'])
                    if match:
                        commit['task'] = '-'.join(map(str, match.groups())).upper()

                        if commit['task'] in show_tasks:
                            commit['show'] = False

                        show_tasks.append(commit['task'])
                        title = jira_client.get_task_title(commit['task'])
                        if title != None:
                            commit['title'] = title

                    commit['message'] = commit['message'].strip()

                if commits:
                    report(general_cfg).commits(project, [i for i in commits if len(i['message']) and i['show']])
        except:
            report(general_cfg).error(project, traceback.format_exc())


    cache.close()

if __name__ == '__main__':
    sys.exit(main())