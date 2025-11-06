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
			frappe.log_error(e, title = _('Twilio WhatsApp Message Error'))
	
	def get_message_dict(self):
		args = {
			'from_': self.from_,
			'to': self.to,
			'body': self.message,
		}
		
		# Only add callback if site is publicly accessible
		site_url = get_url()

		if not site_url.startswith('http://localhost') and not ':800' in site_url:
			args['status_callback'] = '{}/api/method/twilio_integration.twilio_integration.api.whatsapp_message_status_callback'.format(site_url)
		
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
	def send_whatsapp_message(cls, receiver_list, message, doctype, docname, attachments=None):
		if isinstance(receiver_list, string_types):
			receiver_list = loads(receiver_list)
			if not isinstance(receiver_list, list):
				receiver_list = [receiver_list]

		# Handle attachments - upload to accessible location if provided
		media_url = None
		if attachments:
			media_url = cls.handle_attachment(attachments, doctype, docname)
			
			# If media URL is not accessible, add download link to message
			if not media_url:
				frappe.log_error(
					title="WhatsApp Media Warning",
					message="Media URL not generated. PDF will not be attached to WhatsApp message."
				)

		for rec in receiver_list:
			wa_message = cls.store_whatsapp_message(rec, message, doctype, docname, media_url)
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
	def store_whatsapp_message(to, message, doctype=None, docname=None, media=None):
		sender = frappe.db.get_single_value('Twilio Settings', 'whatsapp_no')
		wa_msg = frappe.get_doc({
				'doctype': 'WhatsApp Message',
				'from_': 'whatsapp:{}'.format(sender),
				'to': 'whatsapp:{}'.format(to),
				'message': message,
				'reference_doctype': doctype,
				'reference_document_name': docname,
				'media_link': media
			}).insert(ignore_permissions=True)

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