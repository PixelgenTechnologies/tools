#!/usr/bin/env python
"""Creates a nf-core pipeline matching the current
organization's specification based on a template.
"""
import imghdr
import logging
import os
import pathlib
import random
import re
import shutil
import sys
import time

import git
import jinja2
import questionary
import requests
import yaml

import nf_core
import nf_core.utils

log = logging.getLogger(__name__)


class PipelineCreate(object):
    """Creates a nf-core pipeline a la carte from the nf-core best-practice template.

    Args:
        name (str): Name for the pipeline.
        description (str): Description for the pipeline.
        author (str): Authors name of the pipeline.
        version (str): Version flag. Semantic versioning only. Defaults to `1.0dev`.
        no_git (bool): Prevents the creation of a local Git repository for the pipeline. Defaults to False.
        force (bool): Overwrites a given workflow directory with the same name. Defaults to False.
            May the force be with you.
        outdir (str): Path to the local output directory.
    """

    def __init__(
        self,
        name,
        description,
        author,
        version="1.0dev",
        no_git=False,
        force=False,
        outdir=None,
        template_yaml_path=None,
    ):
        self.template_params = self.create_param_dict(name, description, author, version, outdir, template_yaml_path)

        self.no_git = no_git
        self.force = force
        if outdir is None:
            outdir = os.path.join(os.getcwd(), self.template_params["name_noslash"])
        self.outdir = outdir

    def create_param_dict(self, name, description, author, version, outdir, template_yaml_path):
        """Creates a dictionary of parameters for the new pipeline.

        Args:
            template_yaml_path (str): Path to YAML file containing template parameters.
        """
        if template_yaml_path is not None:
            with open(template_yaml_path, "r") as f:
                template_yaml = yaml.safe_load(f)
        else:
            template_yaml = {}

        param_dict = {}
        # Get the necessary parameters either from the template or command line arguments
        param_dict["name"] = self.get_param("name", name, template_yaml, template_yaml_path)
        param_dict["description"] = self.get_param("description", description, template_yaml, template_yaml_path)
        param_dict["author"] = self.get_param("author", author, template_yaml, template_yaml_path)

        if "version" in template_yaml:
            if version is not None:
                log.info(f"Overriding --version with version found in {template_yaml_path}")
            version = template_yaml["version"]
        param_dict["version"] = version

        # Define the different template areas
        template_areas = [
            {"name": "GitHub CI", "value": "ci"},
            {"name": "GitHub badges", "value": "github_badges"},
            {"name": "iGenomes config", "value": "igenomes"},
        ]

        # Once all necessary parameters are set, check if the user wants to customize the template more
        if template_yaml_path is None:
            customize_template = questionary.confirm(
                "Do you want to customize which parts of the template are used?",
                style=nf_core.utils.nfcore_question_style,
            ).unsafe_ask()
            if customize_template:
                template_yaml.update(self.customize_template(template_areas))

        # Now look in the template for more options, otherwise default to nf-core defaults
        param_dict["prefix"] = template_yaml.get("prefix", "nf-core")

        param_dict["skip"] = []
        for t_area_key in (t_area["value"] for t_area in template_areas):
            param_dict[t_area_key] = t_area_key not in template_yaml.get("skip", [])

        # Set the last parameters based on the ones provided
        param_dict["short_name"] = (
            param_dict["name"].lower().replace(r"/\s+/", "-").replace(f"{param_dict['prefix']}/", "").replace("/", "-")
        )
        param_dict["name"] = f"{param_dict['prefix']}/{param_dict['short_name']}"
        param_dict["name_noslash"] = param_dict["name"].replace("/", "-")
        param_dict["prefix_nodash"] = param_dict["prefix"].replace("-", "")
        param_dict["name_docker"] = param_dict["name"].replace(param_dict["prefix"], param_dict["prefix_nodash"])
        param_dict["logo_light"] = f"{param_dict['name_noslash']}_logo_light.png"
        param_dict["logo_dark"] = f"{param_dict['name_noslash']}_logo_dark.png"
        param_dict["version"] = version
        param_dict["branded"] = param_dict["prefix"] == "nf-core"

        return param_dict

    def customize_template(self, template_areas):
        """Customizes the template parameters.

        Args:
            name (str): Name for the pipeline.
            description (str): Description for the pipeline.
            author (str): Authors name of the pipeline.
        """
        template_yaml = {}
        prefix = questionary.text("Pipeline prefix", style=nf_core.utils.nfcore_question_style).unsafe_ask()
        while not re.match(r"^[a-zA-Z_][a-zA-Z0-9-_]*$", prefix):
            log.error("[red]Pipeline prefix cannot start with digit or hyphen and cannot contain punctuation.[/red]")
            prefix = questionary.text(
                "Please provide a new pipeline prefix", style=nf_core.utils.nfcore_question_style
            ).unsafe_ask()
        template_yaml["prefix"] = prefix

        template_yaml["skip"] = questionary.checkbox(
            "Skip template areas?", choices=template_areas, style=nf_core.utils.nfcore_question_style
        ).unsafe_ask()
        return template_yaml

    def get_param(self, param_name, passed_value, template_yaml, template_yaml_path):
        if param_name in template_yaml:
            if passed_value is not None:
                log.info(f"overriding --{param_name} with name found in {template_yaml_path}")
            passed_value = template_yaml["name"]
        if passed_value is None:
            passed_value = getattr(self, f"prompt_wf_{param_name}")()
        return passed_value

    def prompt_wf_name(self):
        wf_name = questionary.text("Workflow name", style=nf_core.utils.nfcore_question_style).unsafe_ask()
        while not re.match(r"^[a-z]+$", wf_name):
            log.error("[red]Invalid workflow name: must be lowercase without punctuation.")
            wf_name = questionary.text(
                "Please provide a new workflow name", style=nf_core.utils.nfcore_question_style
            ).unsafe_ask()
        return wf_name

    def prompt_wf_description(self):
        wf_description = questionary.text("Description", style=nf_core.utils.nfcore_question_style).unsafe_ask()
        return wf_description

    def prompt_wf_author(self):
        wf_author = questionary.text("Author", style=nf_core.utils.nfcore_question_style).unsafe_ask()
        return wf_author

    def init_pipeline(self):

        """Creates the nf-core pipeline."""

        # Make the new pipeline
        self.render_template()

        # Init the git repository and make the first commit
        if not self.no_git:
            self.git_init_pipeline()

        log.info(
            "[green bold]!!!!!! IMPORTANT !!!!!!\n\n"
            + "[green not bold]If you are interested in adding your pipeline to the nf-core community,\n"
            + "PLEASE COME AND TALK TO US IN THE NF-CORE SLACK BEFORE WRITING ANY CODE!\n\n"
            + "[default]Please read: [link=https://nf-co.re/developers/adding_pipelines#join-the-community]https://nf-co.re/developers/adding_pipelines#join-the-community[/link]"
        )

    def render_template(self):
        """Runs Jinja to create a new nf-core pipeline."""
        log.info(f"Creating new nf-core pipeline: '{self.template_params['name']}'")

        # Check if the output directory exists
        if os.path.exists(self.outdir):
            if self.force:
                log.warning(
                    f"Output directory '{self.template_params['outdir']}' exists - continuing as --force specified"
                )
            else:
                log.error(f"Output directory '{self.template_params['outdir']}' exists!")
                log.info("Use -f / --force to overwrite existing files")
                sys.exit(1)
        else:
            os.makedirs(self.outdir)

        # Run jinja2 for each file in the template folder
        env = jinja2.Environment(
            loader=jinja2.PackageLoader("nf_core", "pipeline-template"), keep_trailing_newline=True
        )
        template_dir = os.path.join(os.path.dirname(__file__), "pipeline-template")
        object_attrs = self.template_params
        object_attrs["nf_core_version"] = nf_core.__version__

        # Can't use glob.glob() as need recursive hidden dotfiles - https://stackoverflow.com/a/58126417/713980
        template_files = list(pathlib.Path(template_dir).glob("**/*"))
        template_files += list(pathlib.Path(template_dir).glob("*"))
        ignore_strs = [".pyc", "__pycache__", ".pyo", ".pyd", ".DS_Store", ".egg"]
        rename_files = {
            "workflows/pipeline.nf": f"workflows/{self.template_params['short_name']}.nf",
            "lib/WorkflowPipeline.groovy": f"lib/Workflow{self.template_params['short_name'][0].upper()}{self.template_params['short_name'][1:]}.groovy",
        }
        # Set the paths to skip according to customization
        skippable_paths = {"ci": ".github/workflows/", "igenomes": "conf/igenomes.config"}

        for template_fn_path_obj in template_files:

            template_fn_path = str(template_fn_path_obj)
            if os.path.isdir(template_fn_path):
                continue
            if any([s in template_fn_path for s in ignore_strs]):
                log.debug(f"Ignoring '{template_fn_path}' in jinja2 template creation")
                continue

            # Set up vars and directories
            template_fn = os.path.relpath(template_fn_path, template_dir)
            output_path = os.path.join(self.outdir, template_fn)
            if template_fn in rename_files:
                output_path = os.path.join(self.outdir, rename_files[template_fn])
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            try:
                # Just copy binary files
                if nf_core.utils.is_file_binary(template_fn_path):
                    raise AttributeError(f"Binary file: {template_fn_path}")

                # Got this far - render the template
                log.debug(f"Rendering template file: '{template_fn}'")
                j_template = env.get_template(template_fn)
                rendered_output = j_template.render(object_attrs)

                # Write to the pipeline output file
                with open(output_path, "w") as fh:
                    log.debug(f"Writing to output file: '{output_path}'")
                    fh.write(rendered_output)

            # Copy the file directly instead of using Jinja
            except (AttributeError, UnicodeDecodeError) as e:
                log.debug(f"Copying file without Jinja: '{output_path}' - {e}")
                shutil.copy(template_fn_path, output_path)

            # Something else went wrong
            except Exception as e:
                log.error(f"Copying raw file as error rendering with Jinja: '{output_path}' - {e}")
                shutil.copy(template_fn_path, output_path)

            # Mirror file permissions
            template_stat = os.stat(template_fn_path)
            os.chmod(output_path, template_stat.st_mode)

        # Make a logo and save it
        self.make_pipeline_logo()

    def make_pipeline_logo(self):
        """Fetch a logo for the new pipeline from the nf-core website"""

        logo_url = f"https://nf-co.re/logo/{self.template_params['short_name']}?theme=light"
        log.debug(f"Fetching logo from {logo_url}")

        email_logo_path = (
            f"{self.template_params['outdir']}/assets/{self.template_params['name_noslash']}_logo_light.png"
        )
        self.download_pipeline_logo(f"{logo_url}&w=400", email_logo_path)
        for theme in ["dark", "light"]:
            readme_logo_url = f"{logo_url}?w=600&theme={theme}"
            readme_logo_path = (
                f"{self.template_params['outdir']}/docs/images/{self.template_params['name_noslash']}_logo_{theme}.png"
            )
            self.download_pipeline_logo(readme_logo_url, readme_logo_path)

    def download_pipeline_logo(self, url, img_fn):
        """Attempt to download a logo from the website. Retry if it fails."""
        os.makedirs(os.path.dirname(img_fn), exist_ok=True)
        attempt = 0
        max_attempts = 10
        retry_delay = 0  # x up to 10 each time, so first delay will be 1-100 seconds
        while attempt < max_attempts:
            # If retrying, wait a while
            if retry_delay > 0:
                log.info(f"Waiting {retry_delay} seconds before next image fetch attempt")
                time.sleep(retry_delay)

            attempt += 1
            # Use a random number to avoid the template sync hitting the website simultaneously for all pipelines
            retry_delay = random.randint(1, 100) * attempt
            log.debug(f"Fetching logo '{img_fn}' (attempt {attempt})")
            try:
                # Try to fetch the logo from the website
                r = requests.get(url, timeout=180)
                if r.status_code != 200:
                    raise UserWarning(f"Got status code {r.status_code}")
                # Check that the returned image looks right

            except (ConnectionError, UserWarning) as e:
                # Something went wrong - try again
                log.warning(e)
                log.error("Connection error - retrying")
                continue

            # Write the new logo to the file
            with open(img_fn, "wb") as fh:
                fh.write(r.content)
            # Check that the file looks valid
            image_type = imghdr.what(img_fn)
            if image_type != "png":
                log.error(f"Logo from the website didn't look like an image: '{image_type}'")
                continue

            # Got this far, presumably it's good - break the retry loop
            break

    def git_init_pipeline(self):
        """Initialises the new pipeline as a Git repository and submits first commit."""
        log.info("Initialising pipeline git repository")
        repo = git.Repo.init(self.outdir)
        repo.git.add(A=True)
        repo.index.commit(f"initial template build from nf-core/tools, version {nf_core.__version__}")
        # Add TEMPLATE branch to git repository
        repo.git.branch("TEMPLATE")
        repo.git.branch("dev")
        log.info(
            "Done. Remember to add a remote and push to GitHub:\n"
            f"[white on grey23] cd {self.template_params['outdir']} \n"
            " git remote add origin git@github.com:USERNAME/REPO_NAME.git \n"
            " git push --all origin                                       "
        )
        log.info("This will also push your newly created dev branch and the TEMPLATE branch for syncing.")
