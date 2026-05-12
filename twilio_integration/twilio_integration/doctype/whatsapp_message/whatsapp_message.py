import frappe
from frappe.model.document import Document
from six import string_types
from json import loads
from frappe.utils.password import get_decrypted_password
from frappe.utils import get_site_url,get_url
from frappe import _
from ...twilio_handler import Twilio
import base64

class WhatsAppMessage(Document):
	def send(self):
		client = Twilio.get_twilio_client()
		message_dict = self.get_message_dict()
		response = frappe._dict()

		try:
			response = client.messages.create(**message_dict)
			self.sent_received = 'Sent'
			self.status = response.status.title()
			self.id = response.sid
			self.send_on = response.date_sent
			self.save(ignore_permissions=True)
		
		except Exception as e:
			self.db_set('status', "Error")
			frappe.log_error(_('Twilio WhatsApp Message Error'),e)
	
	def get_message_dict(self):
		args = {
			'from_': self.from_,
			'to': self.to,
		}
		
		# Only add callback if site is publicly accessible
		site_url = get_url()

		if not site_url.startswith('http://localhost') and not ':800' in site_url:
			args['status_callback'] = '{}/api/method/twilio_integration.twilio_integration.api.whatsapp_message_status_callback'.format(site_url)
		
		# Add WhatsApp template (Content SID) if available
		if self.get('whatsapp_template_id'):
			args['content_sid'] = self.whatsapp_template_id
			
			# Add content_variables if present on the document
			if self.get('variables'):
				import json
				import re
				content_vars = {}
				for row in self.variables:
					val = row.variable_data or ""
					# Convert HTML block elements to newlines BEFORE stripping tags
					# so that paragraph/div/br boundaries become real line breaks
					val = re.sub(r'<br\s*/?>', '\n', val, flags=re.IGNORECASE)
					val = re.sub(r'</p>', '\n', val, flags=re.IGNORECASE)
					val = re.sub(r'</div>', '\n', val, flags=re.IGNORECASE)
					val = re.sub(r'</li>', '\n', val, flags=re.IGNORECASE)
					# Strip all remaining HTML tags
					val = re.sub(r'<[^>]+>', '', val)
					# Collapse horizontal whitespace (spaces/tabs) on each line,
					# but keep newlines intact so WhatsApp renders line breaks
					lines = val.split('\n')
					lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in lines]
					# Drop fully empty lines that appear in runs of 3+ to avoid
					# excessive blank space, but keep single blank lines for spacing
					cleaned_lines = []
					consecutive_blanks = 0
					for line in lines:
						if line == '':
							consecutive_blanks += 1
							if consecutive_blanks <= 1:
								cleaned_lines.append(line)
						else:
							consecutive_blanks = 0
							cleaned_lines.append(line)
					val = '\n'.join(cleaned_lines).strip()
					content_vars[str(row.variable_name)] = val
				args['content_variables'] = json.dumps(content_vars)
				
			frappe.log_error(
				title="WhatsApp Template Used",
				message=f"Using Content Template SID: {self.whatsapp_template_id}\nVariables: {args.get('content_variables')}"
			)
		else:
			args['body'] = self.message
		
		# Handle media_link - ensure it's a string
		if self.media_link:
			# If media_link is a list, take the first item
			if isinstance(self.media_link, list):
				media_url = self.media_link[0] if len(self.media_link) > 0 else None
			else:
				media_url = self.media_link
			
			if media_url:
				args['media_url'] = [media_url]
				frappe.log_error(
					title="WhatsApp Media Link",
					message=f"Sending media: {media_url}"
				)

		return args

	@classmethod
	def send_whatsapp_message(cls, receiver_list, message, doctype, docname, attachments=None, template_id=None, content_variables=None):
		if isinstance(receiver_list, string_types):
			receiver_list = loads(receiver_list)
			if not isinstance(receiver_list, list):
				receiver_list = [receiver_list]

		media_url = None
		if attachments:
			media_url = cls.handle_attachment(attachments, doctype, docname)
			if not media_url:
				frappe.log_error(
					title="WhatsApp Media Warning",
					message="Media URL not generated. PDF will not be attached to WhatsApp message."
				)

		for rec in receiver_list:
			wa_message = cls.store_whatsapp_message(rec, message, doctype, docname, media_url, template_id, content_variables)
			wa_message.send()

	@staticmethod
	def handle_attachment(attachments, doctype, docname):
		"""
		Handle attachment by saving the PDF content and returning its public URL
		"""
		try:
			if not attachments or len(attachments) == 0:
				frappe.log_error(title='WhatsApp Attachment', message="No attachments provided")
				return None
			
			# Get the first attachment
			attachment = attachments[0]
			fname = attachment.get('fname')
			fcontent = attachment.get('fcontent')
			
			if not fcontent:
				frappe.log_error(title='WhatsApp Attachment', message="No file content in attachment")
				return None
			
			frappe.log_error(
				title='WhatsApp Attachment Processing',
				message=f"File: {fname}, Size: {len(fcontent)} bytes"
			)
			
			# Use Frappe's save_file utility
			from frappe.utils.file_manager import save_file
			
			file_doc = save_file(
				fname=fname,
				content=fcontent,
				dt=doctype,
				dn=docname,
				is_private=0,  # Public so Twilio can access
				decode=False
			)
			
			# Get the site URL
			site_url = get_url()

			frappe.log_error("Site URL: {}".format(site_url))
			file_url = f"{site_url}{file_doc.file_url}"
			
			# Check if URL is publicly accessible
			if 'https://' not in site_url or 'localhost' in site_url or ':800' in site_url:
				frappe.log_error(
					'WhatsApp Media URL Warning',
					f"Media URL not publicly accessible: {file_url}\nTwilio requires HTTPS URLs accessible from the internet.\nUse ngrok or deploy to a public server."
				)
				return None
			
			frappe.log_error(
				title='WhatsApp Attachment Saved',
				message=f"File URL (public): {file_url}"
			)
			
			return file_url
			
		except Exception as e:
			frappe.log_error(
				title='WhatsApp Attachment Error',
				message=f"Failed to handle attachment: {str(e)}\n{frappe.get_traceback()}"
			)
			return None

	@staticmethod
	def store_whatsapp_message(to, message, doctype=None, docname=None, media=None, template_id=None, content_variables=None):
		sender = frappe.db.get_single_value('Twilio Settings', 'whatsapp_no')
		
		doc_dict = {
			'doctype': 'WhatsApp Message',
			'from_': 'whatsapp:{}'.format(sender),
			'to': 'whatsapp:{}'.format(to),
			'message': message,
			'reference_doctype': doctype,
			'reference_document_name': docname,
			'media_link': media
		}
		
		if template_id:
			doc_dict['whatsapp_template_id'] = template_id
		
		# Add content_variables if provided
		if content_variables:
			try:
				import json
				parsed_vars = json.loads(content_variables)
				doc_dict['variables'] = []
				for k, v in parsed_vars.items():
					doc_dict['variables'].append({
						'variable_name': k,
						'variable_data': v
					})
			except Exception as e:
				frappe.log_error("Failed to parse content_variables", str(e))

		wa_msg = frappe.get_doc(doc_dict).insert(ignore_permissions=True)
		return wa_msg

def incoming_message_callback(args):
	wa_msg = frappe.get_doc({
			'doctype': 'WhatsApp Message',
			'from_': args.From,
			'to': args.To,
			'message': args.Body,
			'profile_name': args.ProfileName,
			'sent_received': args.SmsStatus.title(),
			'id': args.MessageSid,
			'send_on': frappe.utils.now(),
			'status': 'Received'
		}).insert(ignore_permissions=True)