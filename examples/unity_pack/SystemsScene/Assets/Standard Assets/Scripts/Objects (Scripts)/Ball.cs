using UnityEngine;

public class Ball : MonoBehaviour
{
	void Start ()
	{
		transform.LookAt(Camera.main.transform);
		transform.eulerAngles += Vector3.forward * 45;
		transform.rotation = Quaternion.LookRotation(Vector3.forward, Vector3.up);
	}
	
	void Update ()
	{
		transform.Rotate(Vector3.right * 90 * Time.deltaTime);
	}
}
