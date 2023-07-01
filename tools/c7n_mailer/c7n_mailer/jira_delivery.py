from typing import Dict, List

from c7n_mailer import utils
from jira import JIRA


class JiraDelivery:
    def __init__(self, config, session, logger):
        self.config = config
        self.session = session
        self.logger = logger
        self.url = config.get("jira_url")
        # The tag attached to cloud resources to indicate what Jira project to log ticket to
        self.prj_key = config.get("jira_project_tag", "c7n_jira_project")
        # The dict to set custom fields for each Jira project when needed
        self.custom_fields = config.get("jira_custom_fields", {})
        self.init_jira()

    def init_jira(self):
        token = utils.kms_decrypt(self.config, self.logger, self.session, "jira_token")
        self.logger.info(f'Decrypted jira_token: {token != self.config["jira_token"]}')
        self.config["jira_token"] = token
        # Use basic_auth attr to include both user name and API token
        basic_auth = tuple([self.config.get("jira_username"), token])
        self.client = JIRA(server=self.url, basic_auth=basic_auth)

    def get_project_to_resources(self, message, default) -> Dict[str, List]:
        grouped = {}
        for r in message["resources"]:
            project = utils.get_resource_tag_value(r, self.prj_key) or default
            grouped.setdefault(project, []).append(r)
        # eg: { 'MYPROJECT': [resource1, resource2, etc] }
        return grouped

    def process(self, message) -> List:
        issue_list = []
        jira_conf = message["action"].get("jira", {})
        pr = self.get_project_to_resources(message, jira_conf.get("project"))
        for project, resources in pr.items():
            if not project:
                self.logger.info(f"Skip {len(resources)} resources due to no project value found")
                continue
            self.logger.info(
                "Sending account:%s policy:%s %s:%d jira:%s to %s"
                % (
                    message.get("account", ""),
                    message["policy"]["name"],
                    message["policy"]["resource"],
                    len(resources),
                    message["action"].get("jira_template", "default"),
                    project,
                )
            )
            issue = {}
            # Ref https://jira.readthedocs.io/examples.html#issues
            # Set the dict via action conf from policy pov, via mailer conf from Jira projects pov
            issue.update(**jira_conf)
            issue.update(**self.custom_fields.get("DEFAULT", {}))
            issue.update(**self.custom_fields.get(project, {}))
            # NOTE remove `cannot-be-set` attributes in case some Jira projects can't have them
            [issue.pop(k) for k, v in list(issue.items()) if v == "cannot-be-set"]

            issue["project"] = project
            issue.setdefault("issuetype", {"name": "Task"})
            issue.setdefault("summary", utils.get_message_subject(message))
            issue.setdefault(
                "description",
                utils.get_rendered_jinja(
                    project,
                    message,
                    resources,
                    self.logger,
                    "jira_template",
                    "default",
                    self.config["templates_folders"],
                ),
            )
            self.logger.debug(issue)
            issue_list.append(issue)

        issueIds = self.create_issues(issue_list)
        message["action"]["result"].update(jira_issues=issueIds, jira_url=self.url)
        return issueIds

    def create_issues(self, issue_list) -> List:
        if not issue_list:
            return
        res = self.client.create_issues(field_list=issue_list)
        success_list = [i["issue"].key for i in res if i["status"] == "Success"]
        self.logger.info(f"Created issues {success_list}")
        error_list = [i["error"] for i in res if i["error"]]
        if error_list:
            self.logger.error(f"Failed to create issues {error_list}")
        return success_list
