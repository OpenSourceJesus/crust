using UnityEngine;

public class Ball : MonoBehaviour
{
	void Start ()
	{
		transform.LookAt(Camera.main.transform);
		transform.eulerAngles += Vector3.forward * 45;
		transform.rotation = Quaternion.Euler(Vector3.forward * 45);
	}
	
	void Update ()
	{
		transform.Rotate(Vector3.right * 90 * Time.deltaTime);
	}
}
