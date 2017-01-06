"""
<Program Name>
  verifylib.py

<Author>
  Lukas Puehringer <lukas.puehringer@nyu.edu>

<Started>
  June 28, 2016

<Copyright>
  See LICENSE for licensing information.

<Purpose>

  Provides a library to verify a in_toto final product containing
  a software supply chain layout.

  The library provides functions to:
    - verify signatures of a layout
    - verify signatures of a link
    - verify if the expected command of a step aligns with the actual command
      as recorded in the link metadata file.
    - run inspections (records link metadata)
    - verify product or material matchrules for steps or inspections

"""

import sys
import datetime
import iso8601
import fnmatch
from dateutil import tz

import in_toto.util
import in_toto.runlib
import in_toto.models.layout
import in_toto.models.link
import in_toto.ssl_crypto.keys
from in_toto.exceptions import RuleVerficationFailed
from in_toto.matchrule_validators import check_matchrule_syntax
import in_toto.log as log


def run_all_inspections(layout):
  """
  <Purpose>
    Extracts all inspections from a passed Layout's inspect field and
    iteratively runs each inspections command as defined in the in Inspection's
    run field using in-toto runlib.  This producces link metadata which is
    returned as a dictionary with the according inspection names as keys and
    the Link metadata objects as values.

  <Arguments>
    layout:
            A Layout object which is used to extract the Inpsections.

  <Exceptions>
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    Executes the Inspection command and produces Link metadata.

  <Returns>
    A dictionary containing one Link metadata object per Inspection where
    the key is the Inspection name.
  """
  inspection_links_dict = {}
  for inspection in layout.inspect:
    # XXX LP: What should we record as material/product?
    # Is the current directory a sensible default? In general?
    # If so, we should propably make it a default in run_link
    # We could use matchrule paths
    link = in_toto.runlib.run_link(inspection.name, '.', '.',
        inspection.run.split())
    inspection_links_dict[inspection.name] = link
  return inspection_links_dict

def verify_layout_expiration(layout):
  """
  <Purpose>
    Raises an exception if the passed layout has expired, i.e. if its
    "expire" property is lesser "now".
    Time zone aware datetime objects in UTC+00:00 (Zulu Time) are used.

  <Arguments>
    layout:
            The Layout object to be verified.

  <Exceptions>
    LayoutExpiredError
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    None.

  """
  expire_datetime = iso8601.parse_date(layout.expires)
  if expire_datetime < datetime.datetime.now(tz.tzutc()):
    raise LayoutExpiredError("Layout expired")


def verify_layout_signatures(layout, keys_dict):
  """
  <Purpose>
    Iteratively verifies all signatures of a Layout object using the passed
    keys.

  <Arguments>
    layout:
            A Layout object whose signatures are verified.
    keys_dict:
            A dictionary of keys to verify the signatures conformant with
            ssl_crypto.formats.KEYDICT_SCHEMA.

  <Exceptions>
    Raises an exception if a needed key can not be found in the passed
    keys_dict or if a verification fails.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    Verifies cryptographic Layout signatures.

  """
  layout.verify_signatures(keys_dict)


def verify_link_signatures(link, keys_dict):
  """
  <Purpose>
    Iteratively verifies all signatures of a Link object using the passed
    keys.

  <Arguments>
    link:
            A Link object whose signatures are verified.
    keys_dict:
            A dictionary of keys to verify the signatures conformant with
            ssl_crypto.formats.KEYDICT_SCHEMA.

  <Exceptions>
    Raises an exception if a needed key can not be found in the passed
    keys_dict or if a verification fails.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    Verifies cryptographic Link signatures.

  """
  link.verify_signatures(keys_dict)


def verify_all_steps_signatures(layout, links_dict):
  """
  <Purpose>
    Extracts the Steps of a passed Layout and iteratively verifies the
    the signatures of the Link object related to each Step by the name field.
    The public keys used for verification are also extracted from the Layout.

  <Arguments>
    layout:
            A Layout object whose Steps are extracted and verified.
    links_dict:
            A dictionary of Link metadata objects with Link names as keys.

  <Exceptions>
    Raises an exception if a needed key can not be found in the passed
    keys_dict or if a verification fails.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    Verifies cryptographic Link signatures related to Steps of a Layout.

  """
  for step in layout.steps:
    # Find the according link for this step
    link = links_dict[step.name]

    # Create the dictionary of keys for this step
    keys_dict = {}
    for keyid in step.pubkeys:
      keys_dict[keyid] = layout.keys[keyid]

    # Verify link metadata file's signatures
    verify_link_signatures(link, keys_dict)


def verify_command_alignment(command, expected_command):
  """
  <Purpose>
    Checks if a run command aligns with an expected command. The commands align
    if all of their elements are equal. If alignment fails, a warning is
    printed.

    Note:
      Command alignment is a weak guarantee. Because a functionary can easily
      alias commands.

  <Arguments>
    command:
            A command list, e.g. ["vi", "foo.py"]
    expected_command:
            A command list, e.g. ["make", "install"]

  <Exceptions>
    None.

  <Side Effects>
    Logs warning in case commands do not align.

  """
  # In what case command alignment should fail and how that failure should be
  # propagated has been thoughly discussed in:
  # https://github.com/in-toto/in-toto/issues/46 and
  # https://github.com/in-toto/in-toto/pull/47
  # We chose the simplest solution for now, i.e. Warn if they do not align.

  if command != expected_command:
    log.warning("Run command '{0}' differs from expected command '{1}'"
        .format(command, expected_command))


def verify_all_steps_command_alignment(layout, links_dict):
  """
  <Purpose>
    Iteratively checks if all expected commands as defined in the
    Steps of a Layout align with the actual commands as recorded in the Link
    metadata.

  <Arguments>
    layout:
            A Layout object to extract the expected commands from.
    links_dict:
            A dictionary of Link metadata objects with Link names as keys.

  <Exceptions>
    None.

  <Side Effects>
    None.

  """
  for step in layout.steps:
    # Find the according link for this step
    link = links_dict[step.name]
    command = link.command
    expected_command = step.expected_command.split()
    verify_command_alignment(command, expected_command)


def verify_match_rule(rule, artifact_queue, artifacts, links):
  """
  <Purpose>
    Verifies that target path pattern - 3rd or 5th element of rule - matches
    at least one file in target artifacts. This might conflict with
    general understanding of glob patterns (especially "*").

    Further verifies that each matched target artifact has a corresponding
    source artifact with a matching hash in the passed artifact dictionary and
    that this artifact in also the artifact queue.

    This guarantees that the target artifact was reported by the target Step
    and the step that is being verified reported to use an artifact with the
    same hash, as required by the matchrule.

  <Note>
    In case the explicit ("AS", "<target path pattern>") part of the rule is
    omitted the 3rd element of the rule (path pattern) is used to match
    target and source artifacts implicitly.

  <Arguments>
    rule:
            The rule to be verified. Format is one of:
            ["MATCH", "MATERIAL", "<path pattern>", "FROM", "<step name>"]
            ["MATCH", "PRODUCT", "<path pattern>", "FROM", "<step name>"]
            ["MATCH", "MATERIAL", "<path pattern>", "AS",
                "<target path pattern>", "FROM", "<step name>"]
            ["MATCH", "PRODUCT", "<path pattern>", "AS",
                "<target path pattern>", "FROM", "<step name>"]

    artifact_queue:
            A list of artifact paths that haven't been matched by a previous
            rule yet.

    artifacts:
            A dictionary of artifacts, depending on the list the rule was
            extracted from, materials or products of the step or inspection the
            rule was extracted from.
            The format is:
            {
              <path> : HASHDICTS
            }
             with artifact paths as keys
            and HASHDICTS as values.

    links:
            A dictionary of Link objects with Link names as keys.
            The Link objects relate to Steps.
            The contained materials and products are used as verification target.

  <Exceptions>
    raises FormatError if the rule does not conform with the rule format.
    raises an if a matchrule does not verify.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

    RuleVerficationFailed if the source path is not in the source Link's
    artifacts, or the target link is not found, or the target path is not in
    the target link's artifacts or source path and target path hashes don't
    match.

  <Side Effects>
    Uses fnmatch.filter which translates a glob pattern to re.

  <Returns>
    The artifact queue minus the files that were matched by the rule.

  """
  check_matchrule_syntax(rule)
  path_pattern = rule[2]
  target_path_pattern = rule[4] if len(rule) == 7 else path_pattern
  target_type = rule[1].lower()
  target_name = rule[-1]

  # Extract target artifacts from links
  if target_type == "material":
    target_artifacts = links[target_name].materials
  elif target_type == "product":
    target_artifacts = links[target_name].products

  matched_target_artifacts = fnmatch.filter(target_artifacts.keys(),
      target_path_pattern)

  if not matched_target_artifacts:
    raise RuleVerficationFailed("Rule {0} failed, path pattern '{1}' did not "
      "match any {2}s in target link '{3}'"
      .format(rule, target_path_pattern, target_type, target_name))

  inverted_artifacts = in_toto.util.flatten_and_invert_artifact_dict(artifacts)

  # FIXME: sha256 should not be hardcoded but be a setting instead
  hash_algorithm = "sha256"

  for target_path in matched_target_artifacts:
    match_hash = target_artifacts[target_path][hash_algorithm]
    # Look if there is a source artifact that matches the hash
    try:
      source_path = inverted_artifacts[match_hash]
    except KeyError as e:
      raise RuleVerficationFailed("Rule {0} failed, target hash of '{1}' "
          "could not be found in source artifacts".format(rule, target_path))
    else:
      # The matched source artifact's path must be in the artifact queue
      if source_path not in artifact_queue:
        raise RuleVerficationFailed("Rule {0} failed, target hash of '{1}' "
            "could not be found (was matched before)".format(rule, source_path))

      # and it must match with path pattern.
      elif not fnmatch.filter([source_path], path_pattern):
        raise RuleVerficationFailed("Rule {0} failed, target hash of '{1}' "
          "matches hash of '{2}' in source artifacts but should match '{3}')"
          .format(rule, target_path, source_path, path_pattern))

      else:
        artifact_queue.remove(source_path)

  return artifact_queue


def verify_create_rule(rule, artifact_queue):
  """
  <Purpose>
    Verifies that path pattern - 2nd element of rule - matches at least one
    file in the artifact queue. This might conflict with common understanding of
    glob patterns (especially "*").

    The CREATE rule DOES NOT verify if the artifact has appeared in previous or
    will appear in later steps of the software supply chain.

  <Arguments>
    rule:
            The rule to be verified. Format is ["CREATE", "<path pattern>"]

    artifact_queue:
            A list of artifact paths that were not matched by a previous rule.

  <Exceptions>
    raises an FormatError if the rule does not conform with the rule format.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

    RuleVerficationFailed if nothing is matched in the artifact queue.

  <Side Effects>
    Uses fnmatch.filter which translates a glob pattern to re.

  <Returns>
    The artifact queue minus the files that were matched by the rule.

  """
  check_matchrule_syntax(rule)
  path_pattern = rule[1]
  matched_artifacts = fnmatch.filter(artifact_queue, path_pattern)
  if not matched_artifacts:
    raise RuleVerficationFailed("Rule {0} failed, no artifacts were created"
        .format(rule))

  return list(set(artifact_queue) - set(matched_artifacts))


def verify_delete_rule(rule, artifact_queue):
  """
  <Purpose>
    Verifies that the path pattern - 2nd element of rule - does not match any
    files in the artifact queue.

    The DELETE rule DOES NOT verify if the artifact has appeared in previous or
    will appear in later steps of the software supply chain.

  <Arguments>
    rule:
            The rule to be verified. Format is ["DELETE", "<path pattern>"]

    artifact_queue:
            A list of artifact paths that were not matched by a previous rule.

  <Exceptions>
    raises FormatError if the rule does not conform with the rule format.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

    RuleVerficationFailed if path pattern matches files in artifact queue.

  <Side Effects>
    Uses fnmatch.filter which translates a glob pattern to re.

  <Returns>
    None.
    In contrast to other rule types, the DELETE rule does not
    remove matched files from the artifact queue, because it MUST not match
    files in order to pass.

  """
  check_matchrule_syntax(rule)
  path_pattern = rule[1]
  matched_artifacts = fnmatch.filter(artifact_queue, path_pattern)
  if matched_artifacts:
    raise RuleVerficationFailed("Rule {0} failed, artifacts {1} "
        "were not deleted".format(rule, matched_artifacts))


def verify_item_rules(item_name, rules, artifacts, links):
  """
  <Purpose>
    Iteratively apply passed material or product matchrules to guarantee that
    all artifacts required by a rule are matched and that only artifacts
    required by a rule are matched.

  <Algorithm>
      1. Create an artifact queue (a list of all file names found in artifacts)
      2. For each rule
        a. Artifacts matched by a rule are removed from the artifact queue
           (see note below)
        b. If a rule cannot match the artifacts as specified by the rule
              raise an Exception
        c. If the artifacts queue is not empty after verification of a rule
              continue with the next rule and the updated artifacts queue
           If the artifacts queue is empty
              abort verification
      3. After processing all rules the artifact queue must be empty, if not
              raise an Exception

  <Note>
    Each rule will be applied on the artifacts currently in the queue, that is
    if artifacts were already matched by a previous rule in the list they
    cannot be matched again.

    This can lead to ambiguity in case of conflicting rules, e.g. given a step
    with a reported artifact "foo" and a rule list
    [["CREATE", "foo"], ["DELETE", "foo"]].
    In this case the verification would pass, because
    verify_create_rule would remove the artifact from the artifact queue, which
    would make "foo" appear as deleted for verify_delete_rule.

  <Arguments>
    item_name:
            The name of the item (Step or Inspection) being verified,
            used for user feedback.

    rules:
            The list of rules (material or product matchrules) for the item
            being verified.

    artifacts:
            The artifact dictionary (materials or products) as reported by the
            Link of the item being verified.

    links:
            A dictionary of Link objects with Link names as keys.
            The Link objects relate to Steps.
            The contained materials and products are used as verification target.


  <Exceptions>
    raises FormatError if a rule does not conform with the rule format.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    None.

  """
  # A list of file paths, recorded as artifacts for this item
  artifact_queue = artifacts.keys()
  for rule in rules:
    check_matchrule_syntax(rule)
    if rule[0].lower() == "match":
      artifact_queue = verify_match_rule(rule, artifact_queue, artifacts, links)

    elif rule[0].lower() == "create":
      artifact_queue = verify_create_rule(rule, artifact_queue)

    elif rule[0].lower() == "delete":
      verify_delete_rule(rule, artifact_queue)

    # FIXME: MODIFY rule needs revision
    elif rule[0].lower() == "modify":
      raise Exception("modify rule is currently not implemented.")

    else:
      # FIXME: We should never get here since the rule format was verified before
      raise Exception("Invalid Matchrule", rule)

    if not artifact_queue:
      break

  if artifact_queue:
    raise RuleVerficationFailed("Artifacts {0} were not matched by any rule of "
        "item '{1}'".format(artifact_queue, item_name))


def verify_all_item_rules(items, links, target_links=None):
  """
  <Purpose>
    Iteratively verifies material matchrules and product matchrules of
    passed items (Steps or Inspections).

  <Arguments>
    items:
            A list containing Step or Inspection objects whose material
            and product matchrules will be verified.

    links:
            A dictionary of Link objects with Link names as keys. For each
            passed item (Step or Inspection) to be verified, the related Link
            object is taken from this list.

    target_links: (optional)
            A dictionary of Link objects with Link names as keys. Each Link
            object relates to one Step of the supply chain. The artifacts of
            these links are used as match targets for the the artifacts of the
            items to be verified.
            If omitted, the passed links are also used as target_links.

  <Exceptions>
    raises an Exception if a matchrule does not verify.
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    None.

  """
  if not target_links:
    target_links = links

  for item in items:
    link = links[item.name]
    verify_item_rules(item.name, item.material_matchrules,
        link.materials, target_links)
    verify_item_rules(item.name, item.product_matchrules,
        link.products, target_links)


def in_toto_verify(layout_path, layout_key_paths):
  """
  <Purpose>
    Does entire in-toto supply chain verification of a final product
    by performing the following actions:
        1.  Load root layout as specified by 'layout_path'
        2.  Load root layout signing key(s) (project owner public key(s)) as
            specified by 'layout_key_paths'
        3.  Verify layout signature(s)
        4.  Verify layout expiration
        5.  Load link metadata for every Step defined in the layout
            NOTE: in_toto_verify searches for link metadata files by their step
            name plus the extension '.link' in the current directory.
            E.g. a step of name 'foo' is expected to be found in the current
            directory at 'foo.link'.
        6.  For link metadata signatures
        7.  Verify alignment of defined (Step) and reported (Link) commands
            NOTE: Won't raise exception on mismatch
        8.  Execute Inspection commands
            NOTE: Inspections, similar to Steps executed with 'in-toto-run',
            will record materials before and products after command execution.
            For now it records everything in the current working directory.
        9.  Verify rules defined in each Step's material_matchrules and
            product_matchrules field.
       10.  Verify rules defined in each Inspection's material_matchrules and
            product_matchrules field.

  <Arguments>
    layout_path:
            Path to the layout that is being verified.

    layout_key_paths:
            List of paths to project owner public keys, used to verify the
            layout's signature.

  <Exceptions>
    TBA (see https://github.com/in-toto/in-toto/issues/6)

  <Side Effects>
    Reads layout from disk
    Reads project owner public key(s) from disk
    Reads link metadata files from disk

  """

  log.doing("Verifying software supply chain...")

  log.doing("Reading layout...")
  layout = in_toto.models.layout.Layout.read_from_file(layout_path)

  log.doing("Reading layout key(s)...")
  layout_key_dict = in_toto.util.import_rsa_public_keys_from_files_as_dict(
      layout_key_paths)

  log.doing("Verifying layout signatures...")
  verify_layout_signatures(layout, layout_key_dict)

  log.doing("Verifying layout expiration... ")
  verify_layout_expiration(layout)

  log.doing("Reading link metadata files...")
  step_link_dict = layout.import_step_metadata_from_files_as_dict()

  log.doing("Verifying link metadata signatures...")
  verify_all_steps_signatures(layout, step_link_dict)

  log.doing("Verifying alignment of reported commands...")
  verify_all_steps_command_alignment(layout, step_link_dict)

  log.doing("Executing Inspection commands...")
  inspection_link_dict = run_all_inspections(layout)

  log.doing("Verifying Step rules...")
  verify_all_item_rules(layout.steps, step_link_dict)

  log.doing("Verifying Inspection rules...")
  verify_all_item_rules(layout.inspect, inspection_link_dict,
      step_link_dict)

  # We made it this far without exception that means, verification passed.
  log.passing("Passing verification")
